from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from kopos_connector.kopos.api.fb_orders import (
    get_order_status,
    retry_failed_projections,
    submit_order,
)
from kopos_connector.kopos.tests.frappe_test_fixtures import (
    build_sen_v1_sale_payload,
    create_open_test_shift,
)


class TestFBOrdersAPI(FrappeTestCase):
    def setUp(self):
        frappe.set_user("Administrator")
        self.shift = create_open_test_shift(
            prefix="KOPOS-API-TEST", replenish_stock=False
        )

    def tearDown(self):
        frappe.db.rollback()

    def cleanup_test_data(self):
        frappe.db.rollback()

    def create_test_shift(self):
        return create_open_test_shift(prefix="KOPOS-API-TEST").name

    def test_submit_order_success(self):
        frappe.set_user("Administrator")

        payload = build_sen_v1_sale_payload(self.shift, prefix="KOPOS-API-TEST")

        frappe.local.form_dict = payload

        try:
            result = submit_order()

            self.assertEqual(result["status"], "ok")
            self.assertIn("fb_order", result)
            self.assertIn("sales_invoice", result)
            self.assertIn("ingredient_stock_entry", result)
        except Exception as e:
            self.fail(f"submit_order raised an exception: {e}")

    def test_submit_order_idempotency(self):
        frappe.set_user("Administrator")

        idempotency_key = f"IDEMP-{frappe.generate_hash(length=16)}"

        payload = build_sen_v1_sale_payload(
            self.shift,
            prefix="KOPOS-API-TEST",
            idempotency_key=idempotency_key,
        )

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

        payload = build_sen_v1_sale_payload(self.shift, prefix="KOPOS-API-TEST")

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
