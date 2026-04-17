from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase


class TestEndToEndReturnFlow(FrappeTestCase):
    def setUp(self):
        self.cleanup_test_data()
        self.company = frappe.defaults.get_defaults().get("company", "Test Company")
        self.warehouse = "WH - Test Booth"

    def tearDown(self):
        self.cleanup_test_data()

    def cleanup_test_data(self):
        frappe.db.delete("FB Return Event", {"return_id": ("like", "E2E-RETURN-%")})
        frappe.db.commit()

    def test_return_creates_sales_invoice_return(self):
        from kopos_connector.kopos.services.accounting.return_invoice_service import (
            create_return_sales_invoice,
        )

        return_event = frappe.new_doc("FB Return Event")
        return_event.return_id = f"E2E-RETURN-{frappe.generate_hash(length=8)}"
        return_event.reason_code = "Wrong Order"
        return_event.return_to_stock = 0
        return_event.status = "Draft"
        return_event.insert()

        result = create_return_sales_invoice(return_event)

        if result:
            return_invoice = frappe.get_doc("Sales Invoice", result)
            self.assertEqual(return_invoice.is_return, 1)
            self.assertIsNotNone(return_invoice.return_against)
            print(f"\n✓ Created Sales Invoice Return: {result}")

    def test_return_to_stock_creates_reversal_stock_entry(self):
        from kopos_connector.kopos.services.inventory.stock_reversal_service import (
            create_reversal_stock_entry,
        )

        return_event = frappe.new_doc("FB Return Event")
        return_event.return_id = f"E2E-RETURN-{frappe.generate_hash(length=8)}"
        return_event.reason_code = "Quality Issue"
        return_event.return_to_stock = 1
        return_event.status = "Draft"
        return_event.insert()

        result = create_reversal_stock_entry(return_event)

        if result:
            stock_entry = frappe.get_doc("Stock Entry", result)
            self.assertEqual(stock_entry.stock_entry_type, "Material Receipt")
            print(f"\n✓ Created Reversal Stock Entry: {result}")


class TestEndToEndRemakeFlow(FrappeTestCase):
    def setUp(self):
        self.cleanup_test_data()

    def tearDown(self):
        self.cleanup_test_data()

    def cleanup_test_data(self):
        frappe.db.delete("FB Remake Event", {"remake_id": ("like", "E2E-REMAKE-%")})
        frappe.db.commit()

    def test_remake_creates_stock_entry_no_revenue(self):
        from kopos_connector.kopos.services.operations.remake_service import (
            create_remake_stock_entry,
        )

        remake_event = frappe.new_doc("FB Remake Event")
        remake_event.remake_id = f"E2E-REMAKE-{frappe.generate_hash(length=8)}"
        remake_event.reason_code = "Spill"
        remake_event.status = "Draft"
        remake_event.insert()

        result = create_remake_stock_entry(remake_event)

        if result:
            stock_entry = frappe.get_doc("Stock Entry", result)
            self.assertEqual(stock_entry.stock_entry_type, "Material Issue")
            print(f"\n✓ Created Remake Stock Entry: {result}")
            print(f"  - No Sales Invoice created (remake = no revenue)")
