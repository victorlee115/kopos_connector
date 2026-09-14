"""Synthetic PIN authoring through real Frappe saves; no fixture is committed."""
import frappe
from frappe.tests.utils import FrappeTestCase

from kopos_connector.utils.pin import verify_pin


NON_ASCII_PINS = ["１２３４", "١٢٣٤", "۱۲۳۴", "²³⁴⁵", "𝟙𝟚𝟛𝟜", "12٣4"]


class TestPinPersistenceContract(FrappeTestCase):
    def setUp(self):
        frappe.set_user("Administrator")
        source = frappe.db.get_value("KoPOS Device", {"device_id": "SMOKE-TAB-A001"}, "pos_profile")
        self.assertTrue(source, "Run against the commissioned synthetic smoke fixture")
        self.profile = frappe.copy_doc(frappe.get_doc("POS Profile", source))
        self.profile.name = "PIN-CONTRACT-" + frappe.generate_hash(length=10)
        self.profile.insert(ignore_permissions=True)
        self.device = frappe.get_doc({
            "doctype": "KoPOS Device", "device_id": "PIN-CONTRACT-" + frappe.generate_hash(length=10),
            "device_name": "Synthetic PIN contract tablet", "device_prefix": "PC", "enabled": 1,
            "pos_profile": self.profile.name, "device_users": [{
                "user": "Administrator", "display_name": "Synthetic operator", "active": 1,
                "default_cashier": 1, "pin": "0123", "can_open_shift": 1, "can_close_shift": 1,
            }],
        }).insert(ignore_permissions=True)

    def tearDown(self):
        frappe.db.rollback()

    def test_ascii_pin_save_read_and_explicit_rotation_preserve_the_contract(self):
        self.device.reload()
        original = self.device.device_users[0].pin_hash
        version = self.device.config_version
        self.assertTrue(verify_pin("0123", original))
        self.assertFalse(self.device.device_users[0].pin)
        for _ in range(2):
            self.device.save(ignore_permissions=True)
            self.device.reload()
            self.assertEqual(self.device.device_users[0].pin_hash, original)
            self.assertEqual(self.device.config_version, version)
        self.device.device_users[0].pin = "9876"
        self.device.save(ignore_permissions=True)
        self.device.reload()
        self.assertTrue(verify_pin("9876", self.device.device_users[0].pin_hash))
        self.assertFalse(verify_pin("0123", self.device.device_users[0].pin_hash))
        self.assertFalse(self.device.device_users[0].pin)
        self.assertEqual(self.device.config_version, version + 1)

    def test_legacy_device_authoring_rejects_unicode_without_replacing_saved_credentials(self):
        self.device.reload()
        original = self.device.device_users[0].pin_hash
        version = self.device.config_version
        for raw_pin in NON_ASCII_PINS:
            with self.subTest(codepoints=[ord(character) for character in raw_pin]):
                self.device.device_users[0].pin = raw_pin
                with self.assertRaisesRegex(frappe.ValidationError, "0-9"):
                    self.device.save(ignore_permissions=True)
                frappe.clear_messages()
                self.device.reload()
                self.assertEqual(self.device.device_users[0].pin_hash, original)
                self.assertEqual(self.device.config_version, version)
                self.assertFalse(self.device.device_users[0].pin)
                self.assertTrue(verify_pin("0123", original))

    def test_central_authoring_uses_the_same_validator_and_retains_saved_revision(self):
        gender = frappe.get_doc({
            "doctype": "Gender", "gender": "PIN contract " + frappe.generate_hash(length=10),
        }).insert(ignore_permissions=True)
        employee = frappe.get_doc({
            "doctype": "Employee", "first_name": "Synthetic PIN contract", "gender": gender.name,
            "date_of_birth": "1990-01-01", "date_of_joining": "2020-01-01", "status": "Inactive",
            "company": self.profile.company,
        }).insert(ignore_permissions=True)
        access = frappe.get_doc({
            "doctype": "KoPOS Staff Access", "user": "Administrator", "employee": employee.name,
            "access_level": "Staff", "active": 0, "pin": "0123",
            "outlet_assignments": [{"company": self.profile.company, "outlet": "Synthetic PIN contract",
                                    "warehouse": self.profile.warehouse}],
        }).insert(ignore_permissions=True)
        access.reload()
        original, revision = access.pin_hash, access.revision
        self.assertTrue(verify_pin("0123", original))
        self.assertFalse(access.pin)
        for raw_pin in NON_ASCII_PINS:
            with self.subTest(codepoints=[ord(character) for character in raw_pin]):
                access.pin = raw_pin
                with self.assertRaisesRegex(frappe.ValidationError, "0-9"):
                    access.save(ignore_permissions=True)
                frappe.clear_messages()
                access.reload()
                self.assertEqual(access.pin_hash, original)
                self.assertEqual(access.revision, revision)
                self.assertFalse(access.pin)
        access.pin = "9876"
        access.save(ignore_permissions=True)
        access.reload()
        self.assertTrue(verify_pin("9876", access.pin_hash))
        self.assertEqual(access.revision, revision + 1)
        self.assertFalse(access.pin)
