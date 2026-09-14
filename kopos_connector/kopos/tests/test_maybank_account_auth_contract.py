"""Credential lifecycle through real Frappe, MariaDB and Redis; no provider calls."""
from types import SimpleNamespace
from unittest.mock import Mock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from kopos_connector.services.maybank.client import MaybankClient


class TestMaybankAccountAuthContract(FrappeTestCase):
    def setUp(self):
        frappe.set_user("Administrator")
        self.keys = set()
        self.clients = []
        self.account = self._new_account()

    def tearDown(self):
        for client in self.clients:
            client.session.close()
        for key in self.keys:
            frappe.cache().delete(key)
        frappe.db.rollback()

    def _new_account(self, username=None):
        name = "AUTH-CONTRACT-" + frappe.generate_hash(length=12)
        account = frappe.get_doc({
            "doctype": "Maybank QRPayBiz Account", "display_name": name,
            "username": username or name, "enabled": 0, "environment": "production",
            "encrypted_pin": "1234", "user_type": "corporate",
        })
        account.flags.notify_update = False
        account.insert(ignore_permissions=True)
        return account.reload()

    def _client(self, account=None, outlet="SYNTHETIC-CURRENT"):
        client = MaybankClient.from_account_doc(account or self.account, outlet)
        self.clients.append(client)
        for key in (client._jwt_cache_key(), client._outlet_token_cache_key()):
            if key not in self.keys:
                self.assertIsNone(frappe.cache().get(key), "preserve an unexpected existing key")
                self.keys.add(key)
        return client

    def _rotate(self, pin="9876"):
        account = frappe.get_doc("Maybank QRPayBiz Account", self.account.name)
        account.flags.notify_update = False
        account.encrypted_pin = pin
        account.save(ignore_permissions=True)
        self.account = account.reload()
        return self.account

    def test_same_length_pin_rotation_persists_audit_and_new_cache_revision(self):
        old = self._client()
        original_identity = self.account.provider_device_id
        before = self.account.credential_rotated_at
        self.assertTrue(before)
        old._cache_set(old._outlet_token_cache_key(), "synthetic-old", 3540)
        self._rotate()
        current = self._client()
        self.assertEqual(self.account.get_password("encrypted_pin"), "9876")
        self.assertEqual(self.account.provider_device_id, original_identity)
        self.assertNotEqual(self.account.credential_rotated_at, before)
        self.assertEqual(self.account.credential_rotated_at, frappe.db.get_value(
            "Maybank QRPayBiz Account", self.account.name, "credential_rotated_at"))
        self.assertNotEqual(old._outlet_token_cache_key(), current._outlet_token_cache_key())
        self.assertEqual(current._cache_get(current._outlet_token_cache_key()), "")

    def test_masked_pin_on_ordinary_save_preserves_rotation_audit(self):
        old = self._client()
        before = self.account.credential_rotated_at
        self.account.flags.notify_update = False
        self.account.last_connection_status = "Synthetic configuration check"
        self.account.save(ignore_permissions=True)
        self.account.reload()
        current = self._client()
        self.assertEqual(self.account.credential_rotated_at, before)
        self.assertEqual(self.account.get_password("encrypted_pin"), "1234")
        # Every saved revision is intentionally a cold cache, even a label edit.
        self.assertNotEqual(old._jwt_cache_key(), current._jwt_cache_key())

    def test_late_old_client_cannot_populate_current_revision(self):
        old = self._client()
        self._rotate()
        current = self._client()
        old._cache_set(old._jwt_cache_key(), "synthetic-late-old", 3540)
        old._cache_set(old._outlet_token_cache_key(), "synthetic-late-old", 3540)
        self.assertEqual(current._cache_get(current._jwt_cache_key()), "")
        self.assertEqual(current._cache_get(current._outlet_token_cache_key()), "")
        current._cache_set(current._outlet_token_cache_key(), "synthetic-current", 3540)
        reloaded = self._client(frappe.get_doc("Maybank QRPayBiz Account", self.account.name))
        self.assertEqual(reloaded._cache_get(reloaded._outlet_token_cache_key()), "synthetic-current")
        self.assertTrue(3500 <= frappe.cache().ttl(current._outlet_token_cache_key()) <= 3540)
        self.assertEqual(old._cache_get(old._outlet_token_cache_key()), "synthetic-late-old")

    def test_historical_outlet_uses_current_account_revision_without_profile_scan(self):
        old = self._client(outlet="SYNTHETIC-HISTORICAL")
        old._cache_set(old._outlet_token_cache_key(), "synthetic-old", 3540)
        self._rotate()
        historical = MaybankClient.from_transaction(SimpleNamespace(
            maybank_qrpaybiz_account=self.account.name, outlet_id="SYNTHETIC-HISTORICAL"))
        self.clients.append(historical)
        current = self._client(outlet="SYNTHETIC-HISTORICAL")
        self.assertEqual(historical._outlet_token_cache_key(), current._outlet_token_cache_key())
        self.assertEqual(historical._cache_get(historical._outlet_token_cache_key()), "")
        self.assertNotEqual(current._outlet_token_cache_key(), self._client()._outlet_token_cache_key())

    def test_two_accounts_do_not_share_tokens_for_same_username_and_outlet(self):
        other = self._new_account(username=self.account.username)
        first, second = self._client(), self._client(other)
        first._cache_set(first._outlet_token_cache_key(), "synthetic-first-account", 3540)
        self.assertNotEqual(first._outlet_token_cache_key(), second._outlet_token_cache_key())
        self.assertEqual(second._cache_get(second._outlet_token_cache_key()), "")

    def test_rejected_save_keeps_saved_pin_revision_and_audit(self):
        before = (self.account.modified, self.account.credential_rotated_at)
        old = self._client()
        self.account.provider_device_id = "invalid"
        self.account.encrypted_pin = "9876"
        with self.assertRaises(frappe.ValidationError):
            self.account.save(ignore_permissions=True)
        frappe.clear_messages()
        self.account.reload()
        self.assertEqual((self.account.modified, self.account.credential_rotated_at), before)
        self.assertEqual(self.account.get_password("encrypted_pin"), "1234")
        self.assertEqual(old._jwt_cache_key(), self._client()._jwt_cache_key())

    def test_savepoint_rollback_does_not_expose_aborted_revision_token(self):
        before = (self.account.modified, self.account.credential_rotated_at)
        old = self._client()
        frappe.db.savepoint("auth_revision_contract")
        self._rotate()
        aborted = self._client()
        aborted._cache_set(aborted._outlet_token_cache_key(), "synthetic-aborted", 3540)
        frappe.db.rollback(save_point="auth_revision_contract")
        self.account.reload()
        current = self._client()
        self.assertEqual((self.account.modified, self.account.credential_rotated_at), before)
        self.assertEqual(self.account.get_password("encrypted_pin"), "1234")
        self.assertEqual(current._outlet_token_cache_key(), old._outlet_token_cache_key())
        self.assertEqual(current._cache_get(current._outlet_token_cache_key()), "")
        self.assertNotEqual(current._outlet_token_cache_key(), aborted._outlet_token_cache_key())

    def test_prevalidation_cache_namespace_is_not_reused_or_deleted(self):
        client = self._client()
        for key in (client._jwt_cache_key(), client._outlet_token_cache_key()):
            self.assertIn(":v3:", key)
            legacy_key = key.replace(":v3:", ":v2:", 1)
            self.assertIsNone(frappe.cache().get(legacy_key))
            self.keys.add(legacy_key)
            frappe.cache().setex(legacy_key, 3540, "synthetic-unvalidated")
            self.assertEqual(client._cache_get(key), "")
            self.assertEqual(frappe.cache().get(legacy_key), b"synthetic-unvalidated")

    def test_invalid_provider_tokens_never_enter_real_redis(self):
        client = self._client()
        response = Mock(status_code=200)
        response.json.side_effect = [
            {"status": "QR000", "access_token": 123},
            {"status": "QR000", "access_token": "synthetic-provider-passphrase"},
            {"status": "QR000", "data": [{"outletaccesstoken": {"access_token": None}}]},
            {"status": "QR000", "data": [{"outletaccesstoken": {
                "access_token": "U2FsdGVkX18BAgMEBQYHCLFOl3zZokIMBeSFz8tjmeY="}}]},
        ]
        with patch.object(client.session, "post", return_value=response) as post:
            with self.assertRaises(frappe.ValidationError):
                client._get_jwt()
            frappe.clear_messages()
            self.assertIsNone(frappe.cache().get(client._jwt_cache_key()))
            self.assertEqual(client._get_jwt(), "synthetic-provider-passphrase")
            with self.assertRaises(frappe.ValidationError):
                client._get_outlet_token()
            frappe.clear_messages()
            self.assertIsNone(frappe.cache().get(client._outlet_token_cache_key()))
            self.assertEqual(client._get_outlet_token(), "x")
            self.assertEqual(post.call_count, 4)
        reloaded = self._client()
        with patch.object(reloaded.session, "post") as post:
            self.assertEqual(reloaded._get_outlet_token(), "x")
            post.assert_not_called()


class TestMaybankLegacyAccountMigration(FrappeTestCase):
    def setUp(self):
        frappe.set_user("Administrator")
        self.legacy = frappe.get_single("Maybank Settings")
        self.legacy.update({
            "username": "LEGACY-CONTRACT-" + frappe.generate_hash(length=12),
            "encrypted_pin": "1234", "outlet_id": "SYNTHETIC-LEGACY",
            "enabled": 0, "user_type": "corporate",
            "base_url": "https://emerchant.maybank2u.com.my:8443/api/",
        })
        self.legacy.flags.notify_update = False
        self.legacy.save(ignore_permissions=True)
        self.legacy.reload()

    def tearDown(self):
        frappe.db.rollback()

    def _account(self):
        from kopos_connector.patches.migrate_branch_scoped_maybank_qr import _ensure_account
        name = _ensure_account(self.legacy)
        return frappe.get_doc("Maybank QRPayBiz Account", name)

    def test_native_password_insert_preserves_secret_and_device_identity(self):
        account = self._account()
        self.assertEqual(account.get_password("encrypted_pin"), "1234")
        self.assertTrue(account.is_dummy_password(account.encrypted_pin))
        self.assertTrue(account.credential_rotated_at)
        self.assertEqual(account.provider_device_id, self.legacy.provider_device_id)
        stored = frappe.db.sql(
            "SELECT password, encrypted FROM __Auth WHERE doctype=%s AND name=%s AND fieldname=%s",
            (account.doctype, account.name, "encrypted_pin"), as_dict=True)
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0].encrypted, 1)
        self.assertNotEqual(stored[0].password, "1234")

    def test_normalized_legacy_urls_reuse_the_saved_account(self):
        for url in ("", "https://emerchant.maybank2u.com.my:8443/api"):
            with self.subTest(url=url):
                self.legacy.base_url = url
                first, second = self._account(), self._account()
                self.assertEqual(first.name, second.name)
                self.assertEqual(first.modified, second.modified)
                self.assertEqual(frappe.db.count("Maybank QRPayBiz Account",
                    {"username": self.legacy.username}), 1)

    def test_repeated_migration_does_not_rotate_existing_credentials(self):
        account = self._account()
        account.encrypted_pin = "9876"
        account.save(ignore_permissions=True)
        account.reload()
        before = (account.modified, account.credential_rotated_at, account.provider_device_id)
        repeated = self._account()
        self.assertEqual(repeated.name, account.name)
        self.assertEqual(repeated.get_password("encrypted_pin"), "9876")
        self.assertEqual((repeated.modified, repeated.credential_rotated_at,
            repeated.provider_device_id), before)

    def test_missing_legacy_password_fails_without_creating_an_account(self):
        # Remove only the synthetic fixture credential, inside this transaction.
        frappe.db.sql("DELETE FROM __Auth WHERE doctype=%s AND name=%s AND fieldname=%s",
            ("Maybank Settings", "Maybank Settings", "encrypted_pin"))
        with self.assertRaisesRegex(frappe.ValidationError, "Password not found"):
            self._account()
        frappe.clear_messages()
        self.assertFalse(frappe.db.exists("Maybank QRPayBiz Account",
            {"username": self.legacy.username}))

    def test_singleton_migration_data_path_is_idempotent(self):
        from kopos_connector.patches.migrate_branch_scoped_maybank_qr import execute
        self.assertTrue(frappe.get_meta("Maybank Settings").issingle)
        self.assertFalse(frappe.db.table_exists("Maybank Settings"))
        # Schemas and custom fields are already installed. DDL commits are
        # deliberately not run inside a rollback-only test; the migration data
        # path uses the real DB. Upgrade field creation is proven on restored data.
        with patch.object(frappe, "reload_doc") as reload_doc, patch(
            "kopos_connector.install.install.create_kopos_custom_fields"
        ) as create_fields:
            execute()
            first = self._account()
            profiles = frappe.get_all("POS Profile", fields=["name", "modified",
                "custom_kopos_maybank_qrpaybiz_account", "custom_kopos_maybank_outlet_id"])
            self.assertTrue(any(row.custom_kopos_maybank_qrpaybiz_account == first.name
                and row.custom_kopos_maybank_outlet_id == self.legacy.outlet_id for row in profiles))
            execute()
            self.assertEqual(self._account().modified, first.modified)
            self.assertEqual(frappe.get_all("POS Profile", fields=["name", "modified",
                "custom_kopos_maybank_qrpaybiz_account", "custom_kopos_maybank_outlet_id"]), profiles)
            self.assertEqual(reload_doc.call_count, 4)
            self.assertEqual(create_fields.call_count, 2)

    def test_native_patch_identity_repairs_previously_recorded_noop(self):
        from frappe.modules import patch_handler
        from kopos_connector.patches.migrate_branch_scoped_maybank_qr import execute
        original = "kopos_connector.patches.migrate_branch_scoped_maybank_qr"
        repair = original + " # 2026-09-13 native-singleton-password-repair"
        self.assertIn(repair, patch_handler.get_patches_from_app("kopos_connector"))
        if not patch_handler.executed(original):
            patch_handler.update_patch_log(original)
        already_repaired = bool(patch_handler.executed(repair))
        self.assertIs(frappe.get_attr(repair.split(maxsplit=1)[0] + ".execute"), execute)
        # Keep native selection/DB lookup, substitute only the committing runner.
        with patch.object(patch_handler, "execute_patch", return_value=True) as runner:
            self.assertTrue(patch_handler.run_single(original))
            runner.assert_not_called()
            self.assertTrue(patch_handler.run_single(repair))
            if already_repaired:
                runner.assert_not_called()
            else:
                runner.assert_called_once_with(repair, None, None)
            # Fresh installs have already logged the real repair. Preserve that
            # log and test pending selection with a unique comment identity.
            pending = repair + " synthetic-" + frappe.generate_hash(length=12)
            self.assertFalse(patch_handler.executed(pending))
            runner.reset_mock()
            self.assertTrue(patch_handler.run_single(pending))
            runner.assert_called_once_with(pending, None, None)

    def test_legacy_production_credentials_do_not_reuse_a_sandbox_account(self):
        sandbox = frappe.get_doc({"doctype": "Maybank QRPayBiz Account",
            "display_name": self.legacy.username + "-SANDBOX", "username": self.legacy.username,
            "base_url": self.legacy.base_url, "user_type": self.legacy.user_type,
            "environment": "sandbox", "enabled": 0, "encrypted_pin": "4321",
        }).insert(ignore_permissions=True)
        production = self._account()
        self.assertNotEqual(production.name, sandbox.name)
        self.assertEqual(production.environment, "production")
        self.assertEqual(production.get_password("encrypted_pin"), "1234")
        self.assertEqual(sandbox.reload().get_password("encrypted_pin"), "4321")

    def test_historical_backfill_requires_matching_profile_account_and_outlet(self):
        from kopos_connector.patches.migrate_branch_scoped_maybank_qr import (
            _backfill_transaction_snapshots, _profiles_for_devices,
        )
        account = self._account()
        other = frappe.get_doc({"doctype": account.doctype,
            "display_name": self.legacy.username + "-OTHER", "username": self.legacy.username + "-OTHER",
            "enabled": 0, "encrypted_pin": "4321"}).insert(ignore_permissions=True)
        profiles = _profiles_for_devices()
        profile, devices = next(iter(profiles.items()))
        frappe.db.set_value("POS Profile", profile, {
            "custom_kopos_maybank_qrpaybiz_account": other.name,
            "custom_kopos_maybank_outlet_id": self.legacy.outlet_id,
        }, update_modified=False)
        transaction = frappe.get_doc({"doctype": "Maybank QR Transaction",
            "transaction_refno": self.legacy.username, "status": "pending",
            "sale_amount": "1.00", "sale_amount_sen": 100,
            "device_id": devices[0], "outlet_id": self.legacy.outlet_id,
        }).insert(ignore_permissions=True)
        before = transaction.reload().as_dict()
        _backfill_transaction_snapshots(account.name, profiles)
        self.assertEqual(transaction.reload().as_dict(), before)
        frappe.db.set_value("POS Profile", profile, {
            "custom_kopos_maybank_qrpaybiz_account": account.name,
            "custom_kopos_maybank_outlet_id": "OTHER-OUTLET",
        }, update_modified=False)
        _backfill_transaction_snapshots(account.name, profiles)
        self.assertEqual(transaction.reload().as_dict(), before)
        frappe.db.set_value("POS Profile", profile, "custom_kopos_maybank_outlet_id",
            self.legacy.outlet_id, update_modified=False)
        _backfill_transaction_snapshots(account.name, profiles)
        transaction.reload()
        self.assertEqual(transaction.maybank_qrpaybiz_account, account.name)
        self.assertEqual(transaction.pos_profile, profile)
        for field in ("modified", "transaction_refno", "status", "sale_amount", "sale_amount_sen",
            "device_id", "outlet_id", "fb_order", "sales_invoice"):
            self.assertEqual(transaction.get(field), before.get(field))
        bound = transaction.as_dict()
        _backfill_transaction_snapshots(other.name, profiles)
        self.assertEqual(transaction.reload().as_dict(), bound)
