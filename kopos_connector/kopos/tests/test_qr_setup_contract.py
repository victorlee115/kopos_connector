"""Real profile permissions, QR commissioning and transaction boundaries.

Only synthetic fixture changes; the native commit hook prevents a regression
from committing into this retained test site. All changes roll back afterward.
"""
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from kopos_connector.api import qr_setup
from kopos_connector.services.static_qr_commissioning import commissioned_profile_static_qr_config


QR = '00020201021126410014A0000006150001010650166402091234567895204999953034585802MY5909QRCSDNBHD6005BANGI6304184A'


def _reject_commit():
    frappe.db.before_commit.add(_reject_commit)
    raise RuntimeError('QR setup must not commit the enclosing request')


class TestQrSetupContract(FrappeTestCase):
    def setUp(self):
        frappe.set_user('Administrator')
        frappe.db.before_commit.add(_reject_commit)
        # Error Log is non-transactional in this fixture. Observe diagnostics
        # without adding expected before-code failures to the retained log.
        self.diagnostic_patch = patch('kopos_connector.api.maybank_qr_readiness.log_sanitized_error')
        self.diagnostic = self.diagnostic_patch.start()
        self.users = []
        device_name = frappe.db.get_value('KoPOS Device', {'device_id': 'SMOKE-TAB-A001'}, 'name')
        self.assertTrue(device_name, 'Requires the isolated canonical smoke fixture')
        self.device = frappe.get_doc('KoPOS Device', device_name)
        self.profile = frappe.get_doc('POS Profile', self.device.pos_profile)
        self.bank = frappe.db.get_value('Mode of Payment Account', {'parent': 'DuitNow QR', 'company': self.profile.company}, 'default_account')
        self.assertTrue(self.bank)

    def tearDown(self):
        self.diagnostic_patch.stop()
        frappe.set_user('Administrator')
        frappe.db.rollback()
        for user in self.users:
            frappe.clear_cache(user=user)
        frappe.clear_messages()
        self.diagnostic.assert_not_called()

    def _config(self):
        return {
            'expected_modified': frappe.utils.cstr(self.profile.modified),
            'custom_kopos_static_qr_enabled': 1, 'custom_kopos_static_qr_payload': QR,
            'custom_kopos_manual_qr_suspense_account': self.bank,
            'custom_kopos_automatic_qr_enabled': 0,
        }

    def _business_config(self):
        return (frappe.db.get_value('POS Profile', self.profile.name, '*', as_dict=True),
                frappe.db.get_value('KoPOS Device', self.device.name, '*', as_dict=True))

    def test_preview_derives_verified_identity_without_database_writes(self):
        before = self._business_config()
        preview = qr_setup.get_qr_setup_preview(self.profile.name, self._config())
        self.assertTrue(preview['static_qr']['ready'])
        self.assertTrue(preview['requires_merchant_confirmation'])
        self.assertEqual(preview['static_qr_merchant']['merchant_name'], 'QRCSDNBHD')
        self.assertEqual(preview['mode_of_payment']['account'], self.bank)
        self.assertFalse(preview['changes_are_company_wide'])
        self.assertEqual(self._business_config(), before)

    def test_suspense_ledger_uses_shared_qr_ledger_policy_without_popup(self):
        company = self.profile.company
        receivable = frappe.db.get_value('Account', {'company': company, 'root_type': 'Asset', 'account_type': 'Receivable', 'is_group': 0, 'disabled': 0}, 'name')
        untyped = frappe.db.get_value('Account', {'company': company, 'root_type': 'Asset', 'account_type': '', 'is_group': 0, 'disabled': 0}, 'name')
        self.assertTrue(receivable and untyped, 'Requires the standard ERPNext chart in the smoke company')
        before = self._business_config()
        for account, ready in ((receivable, False), (untyped, True), (self.bank, True)):
            with self.subTest(account=account):
                frappe.clear_messages()
                self.diagnostic.reset_mock()
                config = self._config()
                config['custom_kopos_manual_qr_suspense_account'] = account
                preview = qr_setup.get_qr_setup_preview(self.profile.name, config)
                self.assertEqual(preview['static_qr']['ready'], ready)
                # Readiness reports the lane itself; no queued Frappe popup may leak.
                self.assertEqual(frappe.local.message_log, [])
                self.assertEqual(self.diagnostic.call_count, 0 if ready else 1)
        self.diagnostic.reset_mock()
        self.assertEqual(self._business_config(), before)

    def test_confirmed_save_reloads_metadata_and_retry_does_not_recommission(self):
        config = self._config()
        preview = qr_setup.get_qr_setup_preview(self.profile.name, config)
        config['confirmed_static_qr_sha256'] = preview['static_qr_merchant']['payload_sha256']
        version = frappe.db.get_value('KoPOS Device', self.device.name, 'config_version')
        result = qr_setup.apply_qr_configuration(self.profile.name, config)
        self.assertTrue(result['static_qr']['ready'])
        saved = self.profile.reload()
        metadata = commissioned_profile_static_qr_config(saved, expected_company=saved.company)
        self.assertEqual(metadata['static_qr_payload'], QR)
        self.assertEqual(metadata['static_qr_payload_sha256'], config['confirmed_static_qr_sha256'])
        self.assertTrue(metadata['static_qr_commissioned_at'])
        self.assertEqual(qr_setup.get_qr_setup_preview(saved.name)['static_qr_merchant']['merchant_name'], 'QRCSDNBHD')
        after_version = frappe.db.get_value('KoPOS Device', self.device.name, 'config_version')
        self.assertEqual(after_version, version + 1)
        config['expected_modified'] = result['profile_modified']
        qr_setup.apply_qr_configuration(self.profile.name, config)
        self.assertEqual(commissioned_profile_static_qr_config(self.profile.reload(), expected_company=saved.company), metadata)
        self.assertEqual(frappe.db.get_value('KoPOS Device', self.device.name, 'config_version'), after_version)

    def test_unconfirmed_or_different_qr_does_not_replace_saved_configuration(self):
        before = self._business_config()
        for confirmation in (None, '0' * 64):
            with self.subTest(confirmation=bool(confirmation)):
                config = self._config()
                config['confirmed_static_qr_sha256'] = confirmation
                with self.assertRaises(frappe.ValidationError):
                    qr_setup.apply_qr_configuration(self.profile.name, config)
                self.assertEqual(self._business_config(), before)
                frappe.clear_messages()

    def test_stale_preview_uses_native_optimistic_locking(self):
        old_revision = frappe.utils.cstr(self.profile.modified)
        self.profile.custom_kopos_maybank_outlet_id = 'SYNTHETIC-NEWER-EDIT'
        self.profile.save()
        before = self._business_config()
        with self.assertRaises(frappe.TimestampMismatchError):
            qr_setup.apply_qr_configuration(self.profile.name, {
                'expected_modified': old_revision, 'custom_kopos_maybank_outlet_id': 'SYNTHETIC-STALE-EDIT',
            })
        self.assertEqual(self._business_config(), before)

    def test_response_failure_rolls_back_profile_and_device_but_not_enclosing_work(self):
        marker = frappe.get_doc({'doctype': 'ToDo', 'description': 'SYNTHETIC-QR-SAVEPOINT-' + frappe.generate_hash(length=12)}).insert(ignore_permissions=True)
        before = self._business_config()
        with patch.object(qr_setup, '_mode_of_payment_mapping', side_effect=RuntimeError('SYNTHETIC-PREVIEW-FAILURE')):
            with self.assertRaisesRegex(RuntimeError, 'SYNTHETIC-PREVIEW-FAILURE'):
                qr_setup.apply_qr_configuration(self.profile.name, {
                    'expected_modified': frappe.utils.cstr(self.profile.modified),
                    'custom_kopos_maybank_outlet_id': 'SYNTHETIC-ROLLBACK-EDIT',
                })
        self.assertEqual(self._business_config(), before)
        self.assertTrue(frappe.db.exists('ToDo', marker.name))

    def test_real_user_permission_blocks_another_profile_despite_manager_role(self):
        user_name = 'qr-setup-' + frappe.generate_hash(length=12) + '@example.invalid'
        self.users.append(user_name)
        frappe.get_doc({'doctype': 'User', 'email': user_name, 'first_name': 'Synthetic QR Manager',
            'send_welcome_email': 0, 'user_type': 'System User', 'roles': [{'role': 'Accounts Manager'}]}).insert(ignore_permissions=True)
        other = frappe.copy_doc(self.profile)
        other.name = 'SYNTHETIC-QR-PROFILE-' + frappe.generate_hash(length=12)
        other.insert(ignore_permissions=True)
        frappe.get_doc({'doctype': 'User Permission', 'user': user_name, 'allow': 'POS Profile',
            'for_value': self.profile.name, 'apply_to_all_doctypes': 1}).insert(ignore_permissions=True)
        frappe.clear_cache(user=user_name)
        frappe.set_user(user_name)
        # insert(ignore_permissions=True) retains that flag on its object.
        # Permission evidence must use the ordinary fresh document the API uses.
        other = frappe.get_doc('POS Profile', other.name)
        self.assertIn('Accounts Manager', frappe.get_roles())
        self.assertTrue(self.profile.has_permission('write'))
        self.assertFalse(other.has_permission('read'))
        with self.assertRaises(frappe.PermissionError):
            qr_setup.get_qr_setup_preview(other.name)
        with self.assertRaises(frappe.PermissionError):
            qr_setup.apply_qr_configuration(other.name, {'expected_modified': frappe.utils.cstr(other.modified)})
        # Ordinary allowed-manager access succeeds without ignore_permissions.
        qr_setup.apply_qr_configuration(self.profile.name, {'expected_modified': frappe.utils.cstr(self.profile.modified)})

    def test_preview_accepts_the_native_form_post_method(self):
        from kopos_connector import api
        self.assertIn('POST', frappe.allowed_http_methods_for_whitelisted_func[api.get_qr_setup_preview])

    def test_installed_shortcut_persists_plain_review_and_preview_revision(self):
        from kopos_connector.install.install import ensure_pos_profile_provisioning_script
        # Unlike a request API, this installation helper intentionally commits.
        # Stop only that final commit, then inspect the real native saved row.
        # This is save/read evidence, not a completed installation/migration.
        with self.assertRaisesRegex(RuntimeError, 'QR setup must not commit the enclosing request'):
            ensure_pos_profile_provisioning_script()
        script = frappe.get_doc('Client Script', 'KoPOS POS Profile Provisioning Shortcut').script
        self.assertNotIn('JSON.stringify(preview', script)
        self.assertIn('expected_modified: profile.modified', script)
        self.assertIn('confirmed_static_qr_sha256', script)
        self.assertIn('Company-wide payment accounts are not changed', script)
        from importlib.resources import files
        self.assertEqual(script, files('kopos_connector').joinpath('public/js/pos_profile_setup.js').read_text(encoding='utf-8'))
