"""Read real native Company/Address values; all fixture changes are rolled back."""
import frappe
from frappe.tests.utils import FrappeTestCase

from kopos_connector.api.devices import _receipt_header


class TestReceiptHeaderPersistenceContract(FrappeTestCase):
    def setUp(self):
        self.profile_name = frappe.get_all("KoPOS Device", pluck="pos_profile", limit=1)[0]
        self.profile = frappe.get_doc("POS Profile", self.profile_name)
        self.company = self.profile.company

    def tearDown(self):
        frappe.db.rollback()

    def test_native_address_save_read_and_missing_configuration(self):
        address = frappe.get_doc({
            "doctype": "Address", "address_title": "Receipt Test " + frappe.generate_hash(length=8),
            "address_type": "Billing", "address_line1": "12 Test Street", "address_line2": "Level 2",
            "city": "Kuala Lumpur", "pincode": "50000", "country": "Malaysia",
            "links": [{"link_doctype": "Company", "link_name": self.company}],
        }).insert(ignore_permissions=True)
        versions_before = dict(frappe.get_all("KoPOS Device", filters={"pos_profile": self.profile_name},
                                             fields=["name", "config_version"], as_list=True))
        self.assertTrue(versions_before, "config invalidation proof needs a bound tablet")
        self.profile.company_address = address.name
        self.profile.save(ignore_permissions=True)
        versions_after = dict(frappe.get_all("KoPOS Device", filters={"pos_profile": self.profile_name},
                                            fields=["name", "config_version"], as_list=True))
        self.assertEqual(versions_after, {name: max(version or 0, 1) + 1 for name, version in versions_before.items()})
        profile = frappe.get_doc("POS Profile", self.profile_name)
        header = _receipt_header(profile)
        self.assertEqual(header["business_name"], frappe.db.get_value("Company", self.company, "company_name"))
        self.assertEqual(header["address_lines"], ["12 Test Street", "Level 2", "Kuala Lumpur 50000", "Malaysia"])
        frappe.db.set_value("POS Profile", self.profile_name, "company_address", None)
        self.assertEqual(_receipt_header(frappe.get_doc("POS Profile", self.profile_name))["address_lines"], [])
