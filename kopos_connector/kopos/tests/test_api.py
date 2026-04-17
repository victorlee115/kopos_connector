from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from kopos_connector.kopos.api.fb_orders import (
    get_order_status,
    retry_failed_projections,
    submit_order,
)


class TestFBOrdersAPI(FrappeTestCase):
    def setUp(self):
        self.cleanup_test_data()
        self.company = frappe.defaults.get_defaults().get("company", "Test Company")
        self.warehouse = "WH - Test Booth"
        self.shift = self.create_test_shift()

    def tearDown(self):
        self.cleanup_test_data()

    def cleanup_test_data(self):
        frappe.db.delete("FB Order", {"order_id": ("like", "TEST-%")})
        frappe.db.delete("FB Shift", {"shift_code": ("like", "TEST-%")})
        frappe.db.commit()

    def create_test_shift(self):
        shift = frappe.new_doc("FB Shift")
        shift.shift_code = f"TEST-SHIFT-{frappe.generate_hash(length=8)}"
        shift.device_id = "TEST-DEVICE-001"
        shift.staff_id = frappe.session.user
        shift.warehouse = self.warehouse
        shift.company = self.company
        shift.opening_float = 300.0
        shift.status = "Open"
        shift.insert()
        return shift.name

    def test_submit_order_success(self):
        frappe.set_user("Administrator")

        payload = {
            "order_id": f"TEST-ORDER-{frappe.generate_hash(length=8)}",
            "idempotency_key": f"IDEMP-{frappe.generate_hash(length=16)}",
            "device_id": "TEST-DEVICE-001",
            "shift_id": self.shift,
            "staff_id": frappe.session.user,
            "warehouse": self.warehouse,
            "company": self.company,
            "currency": "MYR",
            "order": {
                "display_number": "A001",
                "order_type": "takeaway",
                "created_at": frappe.utils.now(),
                "items": [
                    {
                        "line_id": "LINE-1",
                        "item_code": "TEST-ITEM",
                        "item_name": "Test Item",
                        "qty": 1,
                        "rate": 10.0,
                        "discount_amount": 0,
                        "modifier_total": 0,
                        "amount": 10.0,
                        "modifiers": [],
                    }
                ],
                "payments": [
                    {
                        "payment_method": "Cash",
                        "amount": 10.0,
                        "tendered_amount": 10.0,
                        "change_amount": 0,
                    }
                ],
            },
        }

        frappe.local.form_dict = payload

        try:
            result = submit_order()

            self.assertEqual(result["status"], "ok")
            self.assertIn("fb_order", result)
            self.assertIn("sales_invoice", result)
            self.assertIn("stock_entry", result)
        except Exception as e:
            self.fail(f"submit_order raised an exception: {e}")

    def test_submit_order_idempotency(self):
        frappe.set_user("Administrator")

        idempotency_key = f"IDEMP-{frappe.generate_hash(length=16)}"

        payload = {
            "order_id": f"TEST-ORDER-{frappe.generate_hash(length=8)}",
            "idempotency_key": idempotency_key,
            "device_id": "TEST-DEVICE-001",
            "shift_id": self.shift,
            "staff_id": frappe.session.user,
            "warehouse": self.warehouse,
            "company": self.company,
            "currency": "MYR",
            "order": {
                "display_number": "A001",
                "order_type": "takeaway",
                "created_at": frappe.utils.now(),
                "items": [
                    {
                        "line_id": "LINE-1",
                        "item_code": "TEST-ITEM",
                        "item_name": "Test Item",
                        "qty": 1,
                        "rate": 10.0,
                        "discount_amount": 0,
                        "modifier_total": 0,
                        "amount": 10.0,
                        "modifiers": [],
                    }
                ],
                "payments": [
                    {
                        "payment_method": "Cash",
                        "amount": 10.0,
                        "tendered_amount": 10.0,
                        "change_amount": 0,
                    }
                ],
            },
        }

        frappe.local.form_dict = payload

        try:
            result1 = submit_order()

            frappe.local.form_dict = payload
            result2 = submit_order()

            self.assertEqual(result1["fb_order"], result2["fb_order"])
            self.assertEqual(result2["status"], "duplicate")
        except Exception as e:
            self.fail(f"submit_order raised an exception: {e}")

    def test_get_order_status(self):
        frappe.set_user("Administrator")

        payload = {
            "order_id": f"TEST-ORDER-{frappe.generate_hash(length=8)}",
            "idempotency_key": f"IDEMP-{frappe.generate_hash(length=16)}",
            "device_id": "TEST-DEVICE-001",
            "shift_id": self.shift,
            "staff_id": frappe.session.user,
            "warehouse": self.warehouse,
            "company": self.company,
            "currency": "MYR",
            "order": {
                "display_number": "A001",
                "order_type": "takeaway",
                "created_at": frappe.utils.now(),
                "items": [
                    {
                        "line_id": "LINE-1",
                        "item_code": "TEST-ITEM",
                        "item_name": "Test Item",
                        "qty": 1,
                        "rate": 10.0,
                        "discount_amount": 0,
                        "modifier_total": 0,
                        "amount": 10.0,
                        "modifiers": [],
                    }
                ],
                "payments": [
                    {
                        "payment_method": "Cash",
                        "amount": 10.0,
                        "tendered_amount": 10.0,
                        "change_amount": 0,
                    }
                ],
            },
        }

        frappe.local.form_dict = payload

        try:
            submit_result = submit_order()
            fb_order_name = submit_result["fb_order"]

            status_result = get_order_status(fb_order_name)

            self.assertEqual(status_result["status"], "ok")
            self.assertEqual(status_result["fb_order"], fb_order_name)
            self.assertIn("invoice_status", status_result)
            self.assertIn("stock_status", status_result)
        except Exception as e:
            self.fail(f"get_order_status raised an exception: {e}")
