from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase


class TestEndToEndSaleFlow(FrappeTestCase):
    def setUp(self):
        self.cleanup_test_data()
        self.company = frappe.defaults.get_defaults().get("company", "Test Company")
        self.warehouse = "WH - Test Booth"
        self.shift = self.create_test_shift()
        self.create_test_items()

    def tearDown(self):
        self.cleanup_test_data()

    def cleanup_test_data(self):
        frappe.db.delete("FB Order", {"order_id": ("like", "E2E-TEST-%")})
        frappe.db.delete("FB Shift", {"shift_code": ("like", "E2E-TEST-%")})
        frappe.db.delete("FB Projection Log", {"source_name": ("like", "FB-ORDER-%")})
        frappe.db.commit()

    def create_test_shift(self):
        shift = frappe.new_doc("FB Shift")
        shift.shift_code = f"E2E-TEST-SHIFT-{frappe.generate_hash(length=8)}"
        shift.device_id = "E2E-TEST-DEVICE"
        shift.staff_id = frappe.session.user
        shift.warehouse = self.warehouse
        shift.company = self.company
        shift.opening_float = 300.0
        shift.status = "Open"
        shift.insert()
        return shift.name

    def create_test_items(self):
        test_items = [
            ("E2E-MATCHA-LATTE", "E2E Matcha Latte", 0),
            ("E2E-MATCHA-POWDER", "E2E Matcha Powder", 1),
            ("E2E-MILK", "E2E Milk", 1),
            ("E2E-CUP", "E2E Cup", 1),
        ]

        for item_code, item_name, is_stock in test_items:
            if not frappe.db.exists("Item", item_code):
                item = frappe.new_doc("Item")
                item.item_code = item_code
                item.item_name = item_name
                item.item_group = "Products"
                item.is_stock_item = is_stock
                item.stock_uom = (
                    "Nos" if not is_stock else ("g" if "POWDER" in item_code else "ml")
                )
                item.insert()

    def create_test_recipe(self):
        if frappe.db.exists("FB Recipe", "E2E-TEST-RECIPE"):
            return frappe.get_doc("FB Recipe", "E2E-TEST-RECIPE")

        recipe = frappe.new_doc("FB Recipe")
        recipe.recipe_code = "E2E-TEST-RECIPE"
        recipe.recipe_name = "E2E Test Matcha Latte"
        recipe.sellable_item = "E2E-MATCHA-LATTE"
        recipe.recipe_type = "Finished Drink"
        recipe.status = "Active"
        recipe.version_no = 1
        recipe.company = self.company
        recipe.default_serving_qty = 1
        recipe.default_serving_uom = "Nos"

        recipe.append(
            "components",
            {
                "item": "E2E-MATCHA-POWDER",
                "component_type": "Ingredient",
                "qty": 18.0,
                "uom": "g",
                "affects_stock": 1,
                "affects_cogs": 1,
            },
        )

        recipe.append(
            "components",
            {
                "item": "E2E-MILK",
                "component_type": "Ingredient",
                "qty": 200.0,
                "uom": "ml",
                "affects_stock": 1,
                "affects_cogs": 1,
            },
        )

        recipe.append(
            "components",
            {
                "item": "E2E-CUP",
                "component_type": "Packaging",
                "qty": 1.0,
                "uom": "Nos",
                "affects_stock": 1,
                "affects_cogs": 1,
            },
        )

        recipe.insert()
        return recipe

    def test_01_complete_sale_flow_creates_sales_invoice_not_pos_invoice(self):
        from kopos_connector.kopos.api.fb_orders import submit_order

        self.create_test_recipe()

        order_id = f"E2E-TEST-ORDER-{frappe.generate_hash(length=8)}"
        idempotency_key = f"E2E-IDEMP-{frappe.generate_hash(length=16)}"

        payload = {
            "order_id": order_id,
            "idempotency_key": idempotency_key,
            "device_id": "E2E-TEST-DEVICE",
            "shift_id": self.shift,
            "staff_id": frappe.session.user,
            "warehouse": self.warehouse,
            "company": self.company,
            "currency": "MYR",
            "order": {
                "display_number": "E001",
                "order_type": "takeaway",
                "created_at": frappe.utils.now(),
                "items": [
                    {
                        "line_id": "E2E-LINE-1",
                        "item_code": "E2E-MATCHA-LATTE",
                        "item_name": "E2E Matcha Latte",
                        "qty": 1,
                        "rate": 12.0,
                        "discount_amount": 0,
                        "modifier_total": 0,
                        "amount": 12.0,
                        "modifiers": [],
                    }
                ],
                "payments": [
                    {
                        "payment_method": "Cash",
                        "amount": 12.0,
                        "tendered_amount": 15.0,
                        "change_amount": 3.0,
                    }
                ],
            },
        }

        frappe.local.form_dict = payload
        result = submit_order()

        self.assertEqual(result["status"], "ok")
        self.assertIn("fb_order", result)
        self.assertIn("sales_invoice", result)
        self.assertIsNotNone(result["sales_invoice"])

        fb_order = frappe.get_doc("FB Order", result["fb_order"])
        sales_invoice_name = fb_order.sales_invoice

        self.assertTrue(
            frappe.db.exists("Sales Invoice", sales_invoice_name),
            f"Expected Sales Invoice to exist: {sales_invoice_name}",
        )

        self.assertFalse(
            frappe.db.exists("POS Invoice", sales_invoice_name),
            f"Should NOT create POS Invoice: {sales_invoice_name}",
        )

        sales_invoice = frappe.get_doc("Sales Invoice", sales_invoice_name)
        self.assertEqual(sales_invoice.is_pos, 1)
        self.assertEqual(sales_invoice.update_stock, 0)

        print(f"\n✓ Created Sales Invoice: {sales_invoice_name}")
        print(f"  - is_pos: {sales_invoice.is_pos}")
        print(f"  - update_stock: {sales_invoice.update_stock}")

    def test_02_sales_invoice_has_correct_custom_fields(self):
        from kopos_connector.kopos.api.fb_orders import submit_order

        self.create_test_recipe()

        order_id = f"E2E-TEST-ORDER-{frappe.generate_hash(length=8)}"
        idempotency_key = f"E2E-IDEMP-{frappe.generate_hash(length=16)}"

        payload = {
            "order_id": order_id,
            "idempotency_key": idempotency_key,
            "device_id": "E2E-TEST-DEVICE-002",
            "shift_id": self.shift,
            "staff_id": frappe.session.user,
            "warehouse": self.warehouse,
            "company": self.company,
            "currency": "MYR",
            "order": {
                "display_number": "E002",
                "order_type": "takeaway",
                "created_at": frappe.utils.now(),
                "items": [
                    {
                        "line_id": "E2E-LINE-2",
                        "item_code": "E2E-MATCHA-LATTE",
                        "item_name": "E2E Matcha Latte",
                        "qty": 2,
                        "rate": 12.0,
                        "discount_amount": 0,
                        "modifier_total": 0,
                        "amount": 24.0,
                        "modifiers": [],
                    }
                ],
                "payments": [
                    {
                        "payment_method": "Cash",
                        "amount": 24.0,
                        "tendered_amount": 30.0,
                        "change_amount": 6.0,
                    }
                ],
            },
        }

        frappe.local.form_dict = payload
        result = submit_order()

        fb_order = frappe.get_doc("FB Order", result["fb_order"])
        sales_invoice = frappe.get_doc("Sales Invoice", fb_order.sales_invoice)

        meta = frappe.get_meta("Sales Invoice")
        custom_fields = [
            "custom_fb_order",
            "custom_fb_shift",
            "custom_fb_device_id",
            "custom_fb_event_project",
            "custom_fb_idempotency_key",
            "custom_fb_operational_status",
        ]

        for field in custom_fields:
            if meta.has_field(field):
                value = getattr(sales_invoice, field, None)
                print(f"  - {field}: {value}")

        self.assertEqual(getattr(sales_invoice, "custom_fb_order", None), fb_order.name)

    def test_03_stock_entry_created_for_ingredients(self):
        from kopos_connector.kopos.api.fb_orders import submit_order

        self.create_test_recipe()

        order_id = f"E2E-TEST-ORDER-{frappe.generate_hash(length=8)}"
        idempotency_key = f"E2E-IDEMP-{frappe.generate_hash(length=16)}"

        payload = {
            "order_id": order_id,
            "idempotency_key": idempotency_key,
            "device_id": "E2E-TEST-DEVICE",
            "shift_id": self.shift,
            "staff_id": frappe.session.user,
            "warehouse": self.warehouse,
            "company": self.company,
            "currency": "MYR",
            "order": {
                "display_number": "E003",
                "order_type": "takeaway",
                "created_at": frappe.utils.now(),
                "items": [
                    {
                        "line_id": "E2E-LINE-3",
                        "item_code": "E2E-MATCHA-LATTE",
                        "item_name": "E2E Matcha Latte",
                        "qty": 1,
                        "rate": 12.0,
                        "amount": 12.0,
                        "modifiers": [],
                    }
                ],
                "payments": [
                    {
                        "payment_method": "Cash",
                        "amount": 12.0,
                        "tendered_amount": 15.0,
                        "change_amount": 3.0,
                    }
                ],
            },
        }

        frappe.local.form_dict = payload
        result = submit_order()

        fb_order = frappe.get_doc("FB Order", result["fb_order"])

        self.assertIsNotNone(fb_order.ingredient_stock_entry)
        self.assertTrue(
            frappe.db.exists("Stock Entry", fb_order.ingredient_stock_entry)
        )

        stock_entry = frappe.get_doc("Stock Entry", fb_order.ingredient_stock_entry)
        self.assertEqual(stock_entry.stock_entry_type, "Material Issue")
        self.assertEqual(stock_entry.purpose, "Material Issue")

        print(f"\n✓ Created Stock Entry: {fb_order.ingredient_stock_entry}")
        print(f"  - Type: {stock_entry.stock_entry_type}")
        print(f"  - Items count: {len(stock_entry.items)}")

    def test_04_projection_logs_created(self):
        from kopos_connector.kopos.api.fb_orders import submit_order

        self.create_test_recipe()

        order_id = f"E2E-TEST-ORDER-{frappe.generate_hash(length=8)}"
        idempotency_key = f"E2E-IDEMP-{frappe.generate_hash(length=16)}"

        payload = {
            "order_id": order_id,
            "idempotency_key": idempotency_key,
            "device_id": "E2E-TEST-DEVICE",
            "shift_id": self.shift,
            "staff_id": frappe.session.user,
            "warehouse": self.warehouse,
            "company": self.company,
            "currency": "MYR",
            "order": {
                "display_number": "E004",
                "order_type": "takeaway",
                "created_at": frappe.utils.now(),
                "items": [
                    {
                        "line_id": "E2E-LINE-4",
                        "item_code": "E2E-MATCHA-LATTE",
                        "item_name": "E2E Matcha Latte",
                        "qty": 1,
                        "rate": 12.0,
                        "amount": 12.0,
                        "modifiers": [],
                    }
                ],
                "payments": [
                    {
                        "payment_method": "Cash",
                        "amount": 12.0,
                        "tendered_amount": 15.0,
                        "change_amount": 3.0,
                    }
                ],
            },
        }

        frappe.local.form_dict = payload
        result = submit_order()

        fb_order = frappe.get_doc("FB Order", result["fb_order"])

        logs = frappe.get_all(
            "FB Projection Log",
            filters={"source_doctype": "FB Order", "source_name": fb_order.name},
            fields=["projection_type", "state", "target_name"],
        )

        self.assertTrue(len(logs) >= 1)

        projection_types = [log.projection_type for log in logs]
        self.assertIn("Sales Invoice", projection_types)

        print(f"\n✓ Created {len(logs)} projection logs:")
        for log in logs:
            print(f"  - {log.projection_type}: {log.state} → {log.target_name}")

    def test_05_resolved_sale_created_with_components(self):
        from kopos_connector.kopos.api.fb_orders import submit_order

        self.create_test_recipe()

        order_id = f"E2E-TEST-ORDER-{frappe.generate_hash(length=8)}"
        idempotency_key = f"E2E-IDEMP-{frappe.generate_hash(length=16)}"

        payload = {
            "order_id": order_id,
            "idempotency_key": idempotency_key,
            "device_id": "E2E-TEST-DEVICE",
            "shift_id": self.shift,
            "staff_id": frappe.session.user,
            "warehouse": self.warehouse,
            "company": self.company,
            "currency": "MYR",
            "order": {
                "display_number": "E005",
                "order_type": "takeaway",
                "created_at": frappe.utils.now(),
                "items": [
                    {
                        "line_id": "E2E-LINE-5",
                        "item_code": "E2E-MATCHA-LATTE",
                        "item_name": "E2E Matcha Latte",
                        "qty": 1,
                        "rate": 12.0,
                        "amount": 12.0,
                        "modifiers": [],
                    }
                ],
                "payments": [
                    {
                        "payment_method": "Cash",
                        "amount": 12.0,
                        "tendered_amount": 15.0,
                        "change_amount": 3.0,
                    }
                ],
            },
        }

        frappe.local.form_dict = payload
        result = submit_order()

        fb_order = frappe.get_doc("FB Order", result["fb_order"])

        order_line = fb_order.items[0]
        self.assertIsNotNone(order_line.resolved_sale)

        resolved_sale = frappe.get_doc("FB Resolved Sale", order_line.resolved_sale)

        self.assertEqual(resolved_sale.sellable_item, "E2E-MATCHA-LATTE")
        self.assertEqual(resolved_sale.qty, 1.0)
        self.assertEqual(len(resolved_sale.resolved_components), 3)

        component_items = [c.item for c in resolved_sale.resolved_components]
        self.assertIn("E2E-MATCHA-POWDER", component_items)
        self.assertIn("E2E-MILK", component_items)
        self.assertIn("E2E-CUP", component_items)

        print(f"\n✓ Created Resolved Sale: {resolved_sale.name}")
        print(f"  - Components: {len(resolved_sale.resolved_components)}")
        for comp in resolved_sale.resolved_components:
            print(f"    - {comp.item}: {comp.qty} {comp.uom}")
