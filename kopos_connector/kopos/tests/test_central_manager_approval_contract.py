"""Manager approval follows the staff access the tablet is sent; synthetic records, rolled back."""
import frappe
from frappe.tests.utils import FrappeTestCase

from kopos_connector.kopos.services.inventory_autopilot.staff_access import (
    resolve_staff_access_for_device,
)
from kopos_connector.utils.manager_approval import (
    ManagerApprovalTokenVerificationError,
    _validate_manager_action_authorization_at_use,
    authorize_manager_for_device,
)


LEGACY_PIN, CENTRAL_PIN, ROTATED_PIN = "1357", "2468", "8642"


class TestCentralManagerApprovalContract(FrappeTestCase):
    def setUp(self):
        frappe.set_user("Administrator")
        source = frappe.db.get_value("KoPOS Device", {"device_id": "SMOKE-TAB-A001"}, "pos_profile")
        self.assertTrue(source, "Run against the commissioned synthetic smoke fixture")
        self.profile = frappe.copy_doc(frappe.get_doc("POS Profile", source))
        self.profile.name = "APPROVAL-CONTRACT-" + frappe.generate_hash(length=10)
        self.profile.insert(ignore_permissions=True)
        self.gender = frappe.get_doc({
            "doctype": "Gender", "gender": "Approval contract " + frappe.generate_hash(length=10),
        }).insert(ignore_permissions=True).name
        self.legacy_manager = self._user("Legacy manager")
        self.central_manager = self._user("Central manager")
        self.device = frappe.get_doc({
            "doctype": "KoPOS Device", "device_id": "APPROVAL-CONTRACT-" + frappe.generate_hash(length=10),
            "device_name": "Synthetic approval tablet", "device_prefix": "AC", "enabled": 1,
            "pos_profile": self.profile.name, "device_users": [{
                "user": self.legacy_manager, "display_name": "Legacy manager", "active": 1,
                "default_cashier": 1, "pin": LEGACY_PIN, "can_manager_override": 1, "can_refund": 1,
                "can_void": 1, "can_open_shift": 1, "can_close_shift": 1,
            }],
        }).insert(ignore_permissions=True)

    def tearDown(self):
        frappe.set_user("Administrator")
        frappe.db.rollback()

    def _user(self, first_name):
        email = "approval-" + frappe.generate_hash(length=12) + "@example.invalid"
        frappe.get_doc({
            "doctype": "User", "email": email, "first_name": first_name,
            "user_type": "System User", "send_welcome_email": 0,
        }).insert(ignore_permissions=True)
        return email

    def _grant(self, user, pin, level="Manager"):
        employee = frappe.get_doc({
            "doctype": "Employee", "first_name": "Synthetic approval", "gender": self.gender,
            "date_of_birth": "1990-01-01", "date_of_joining": "2020-01-01", "status": "Active",
            "company": self.profile.company, "user_id": user,
        }).insert(ignore_permissions=True)
        return frappe.get_doc({
            "doctype": "KoPOS Staff Access", "user": user, "employee": employee.name,
            "access_level": level, "active": 1, "pin": pin,
            "outlet_assignments": [{"company": self.profile.company, "outlet": self.profile.name,
                                    "warehouse": self.profile.warehouse}],
        }).insert(ignore_permissions=True)

    def _tablet_managers(self):
        self.device.reload()
        return {row["user"] for row in resolve_staff_access_for_device(self.device) if row["can_manager_override"]}

    def _approves(self, user, pin, action="void_order"):
        try:
            return authorize_manager_for_device(self.device, manager_id=user, manager_pin=pin, action=action) == user
        except frappe.ValidationError:
            return False
        finally:
            frappe.clear_messages()

    def _still_authorized_at_use(self, user, action="void_order"):
        try:
            _validate_manager_action_authorization_at_use(
                device_id=self.device.device_id, manager_id=user, action=action,
                authorization_mode="device_manager",
            )
            return True
        except ManagerApprovalTokenVerificationError:
            return False

    def test_outlet_without_central_access_still_approves_from_device_rows(self):
        self.assertEqual(self._tablet_managers(), {self.legacy_manager})
        self.assertTrue(self._approves(self.legacy_manager, LEGACY_PIN))
        self.assertTrue(self._still_authorized_at_use(self.legacy_manager))
        self.assertFalse(self._approves(self.central_manager, CENTRAL_PIN))

    def test_central_outlet_approves_exactly_the_managers_the_tablet_shows(self):
        access = self._grant(self.central_manager, CENTRAL_PIN)
        self.assertEqual(self._tablet_managers(), {self.central_manager})
        for action in ("void_order", "refund_order", "close_shift"):
            self.assertTrue(self._approves(self.central_manager, CENTRAL_PIN, action), action)
        for action in ("void_order", "refund_order"):
            self.assertTrue(self._still_authorized_at_use(self.central_manager, action), action)
        # The legacy row is still active with its PIN, but the tablet no longer shows that manager.
        self.assertTrue(self.device.device_users[0].active)
        self.assertFalse(self._approves(self.legacy_manager, LEGACY_PIN))
        self.assertFalse(self._still_authorized_at_use(self.legacy_manager))

        access.reload()
        access.access_level = "Staff"
        access.save(ignore_permissions=True)
        self.assertEqual(self._tablet_managers(), set())
        self.assertFalse(self._approves(self.central_manager, CENTRAL_PIN))
        self.assertFalse(self._still_authorized_at_use(self.central_manager))

    def test_central_pin_change_and_deactivation_decide_approval(self):
        access = self._grant(self.legacy_manager, ROTATED_PIN)
        self.assertEqual(self._tablet_managers(), {self.legacy_manager})
        self.assertTrue(self._approves(self.legacy_manager, ROTATED_PIN))
        self.assertFalse(self._approves(self.legacy_manager, LEGACY_PIN))

        access.reload()
        access.active = 0
        access.save(ignore_permissions=True)
        self.assertEqual(self._tablet_managers(), set())
        self.assertTrue(self.device.device_users[0].active)
        self.assertFalse(self._approves(self.legacy_manager, ROTATED_PIN))
        self.assertFalse(self._approves(self.legacy_manager, LEGACY_PIN))
        self.assertFalse(self._still_authorized_at_use(self.legacy_manager))
