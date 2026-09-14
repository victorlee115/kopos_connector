from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase


class TestBroadFixtureIsolation(FrappeTestCase):
    """Keep broad tests from poisoning the subsequent business-state smoke."""

    def tearDown(self) -> None:
        frappe.db.rollback()

    def test_standard_provisioning_page_loads_native_assets_for_system_managers_only(self) -> None:
        from frappe.desk.desk_page import get

        frappe.set_user("Administrator")
        page = frappe.get_doc("Page", "kopos_provisioning")
        self.assertEqual(page.standard, "Yes")
        self.assertEqual(page.module, "KoPOS")
        self.assertEqual([row.role for row in page.roles], ["System Manager"])
        path = Path(frappe.get_module_path("KoPOS", "page", page.name, page.name + ".js"))
        script = path.read_text(encoding="utf-8")
        self.assertEqual(get(page.name).script, script + "\n\n//# sourceURL=kopos_provisioning.js")
        try:
            for role, permitted in (("Company Director", False), ("System Manager", True)):
                frappe.set_user("Administrator")
                email = "page-" + frappe.generate_hash(length=12) + "@example.invalid"
                frappe.get_doc({
                    "doctype": "User", "email": email, "first_name": "Synthetic Page Access",
                    "user_type": "System User", "send_welcome_email": 0, "roles": [{"role": role}],
                }).insert(ignore_permissions=True)
                frappe.set_user(email)
                if permitted:
                    self.assertEqual(get(page.name).script, script + "\n\n//# sourceURL=kopos_provisioning.js")
                else:
                    with self.assertRaises(frappe.PermissionError):
                        get(page.name)
            frappe.set_user("Guest")
            with self.assertRaises(frappe.PermissionError):
                get(page.name)
        finally:
            frappe.set_user("Administrator")
            frappe.response.pop("403", None)

    def test_support_sidebar_links_to_the_native_provisioning_page(self) -> None:
        frappe.set_user("Administrator")
        sidebar = frappe.get_doc("Workspace Sidebar", "KoPOS Support")
        self.assertTrue(any(row.link_to == "kopos_provisioning" for row in sidebar.items))
        self.assertTrue(frappe.db.exists("Page", "kopos_provisioning"))
        icon = frappe.get_doc("Desktop Icon", "KoPOS Support")
        self.assertEqual((icon.link_type, icon.link_to), ("Workspace Sidebar", sidebar.name))
        self.assertEqual([row.role for row in icon.roles], ["System Manager"])
        for document in (sidebar, icon):
            self.assertEqual(document.standard, 1)
            self.assertEqual(document.app, "kopos_connector")

    def test_native_support_import_is_repeatable_and_preserves_personal_sidebar(self) -> None:
        from frappe.modules.import_file import import_file_by_path

        frappe.set_user("Administrator")
        sidebar = frappe.get_doc("Workspace Sidebar", "KoPOS Support")
        personal = frappe.copy_doc(sidebar)
        personal.title = "KoPOS Support-Administrator"
        personal.for_user = "Administrator"
        personal.standard = 0
        personal.app = None
        personal.items[0].label = "Retain my personal shortcut"
        personal.insert(ignore_permissions=True)
        tables = ("Workspace Sidebar", "Workspace Sidebar Item", "Desktop Icon", "Has Role")
        before = {table: frappe.get_all(table, fields=["*"], order_by="name") for table in tables}
        for _ in range(2):
            for folder in ("workspace_sidebar", "desktop_icon"):
                imported = import_file_by_path(frappe.get_app_path("kopos_connector", folder, "kopos_support.json"))
                self.assertFalse(imported)
            self.assertEqual({table: frappe.get_all(table, fields=["*"], order_by="name") for table in tables}, before)

    def test_provisioning_scripts_preserve_unchanged_rows_and_correct_once(self) -> None:
        from kopos_connector.install.install import (
            ensure_kopos_device_provisioning_script,
            ensure_pos_profile_provisioning_script,
        )

        frappe.set_user("Administrator")
        for ensure, name in (
            (ensure_kopos_device_provisioning_script, "KoPOS Device Provisioning Shortcut"),
            (ensure_pos_profile_provisioning_script, "KoPOS POS Profile Provisioning Shortcut"),
        ):
            before = frappe.get_all("Client Script", filters={"name": name}, fields=["*"])
            self.assertEqual(len(before), 1)
            # Keep real saves and validation; prevent this install hook's
            # explicit commit from escaping the existing rollback fixture.
            with patch.object(frappe.db, "commit"):
                for _ in range(2):
                    ensure()
                    self.assertEqual(frappe.get_all("Client Script", filters={"name": name}, fields=["*"]), before)
                document = frappe.get_doc("Client Script", name)
                document.script = "// synthetic stale script"
                document.save(ignore_permissions=True)
                ensure()
                corrected = frappe.get_all("Client Script", filters={"name": name}, fields=["*"])
                self.assertEqual(corrected[0].script, before[0].script)
                for _ in range(2):
                    ensure()
                    self.assertEqual(frappe.get_all("Client Script", filters={"name": name}, fields=["*"]), corrected)

    def test_business_role_warning_excludes_only_native_administrator(self) -> None:
        from kopos_connector.install.install import ensure_jiji_business_module_profile

        frappe.set_user("Administrator")
        roles = set(frappe.get_roles("Administrator"))
        self.assertTrue({"Company Director", "System Manager"}.issubset(roles))
        with patch.object(frappe, "log_error") as warning:
            ensure_jiji_business_module_profile()
            warning.assert_not_called()
            email = "mixed-" + frappe.generate_hash(length=12) + "@example.invalid"
            frappe.get_doc({
                "doctype": "User", "email": email, "first_name": "Synthetic Mixed Roles",
                "user_type": "System User", "send_welcome_email": 0,
                "roles": [{"role": "Company Director"}, {"role": "System Manager"}],
            }).insert(ignore_permissions=True)
            warning.reset_mock()
            ensure_jiji_business_module_profile()
            warning.assert_called_once_with(
                title="JiJi business and support accounts must be separated", message=email,
            )

    def test_price_list_noop_preserves_the_list_and_every_item_price(self) -> None:
        from kopos_connector.smoke import SMOKE_SELLING_PRICE_LIST, _ensure_selling_price_list

        frappe.set_user("Administrator")
        before_list = frappe.get_all("Price List", filters={"name": SMOKE_SELLING_PRICE_LIST}, fields=["*"])
        before_prices = frappe.get_all("Item Price", fields=["*"], order_by="name")
        self.assertEqual(len(before_list), 1)
        self.assertTrue(before_prices)
        for _ in range(2):
            self.assertEqual(_ensure_selling_price_list("MYR"), SMOKE_SELLING_PRICE_LIST)
            self.assertEqual(frappe.get_all("Price List", filters={"name": SMOKE_SELLING_PRICE_LIST}, fields=["*"]), before_list)
            self.assertEqual(frappe.get_all("Item Price", fields=["*"], order_by="name"), before_prices)

    def test_price_list_correction_retains_native_child_updates_then_stops_writing(self) -> None:
        from kopos_connector.smoke import SMOKE_SELLING_PRICE_LIST, _ensure_selling_price_list

        frappe.set_user("Administrator")
        doc = frappe.get_doc("Price List", SMOKE_SELLING_PRICE_LIST)
        doc.buying = 1
        doc.save(ignore_permissions=True)
        filters = {"price_list": SMOKE_SELLING_PRICE_LIST}
        prices = frappe.get_all("Item Price", filters=filters, fields=["*"])
        self.assertTrue(prices)
        self.assertTrue(all(row.buying == 1 for row in prices))
        self.assertEqual(_ensure_selling_price_list("MYR"), SMOKE_SELLING_PRICE_LIST)
        corrected_list = frappe.get_all("Price List", filters={"name": SMOKE_SELLING_PRICE_LIST}, fields=["*"])
        corrected_prices = frappe.get_all("Item Price", fields=["*"], order_by="name")
        self.assertEqual(corrected_list[0].buying, 0)
        self.assertTrue(all(row.buying == 0 for row in corrected_prices if row.price_list == SMOKE_SELLING_PRICE_LIST))
        for _ in range(2):
            self.assertEqual(_ensure_selling_price_list("MYR"), SMOKE_SELLING_PRICE_LIST)
            self.assertEqual(frappe.get_all("Price List", filters={"name": SMOKE_SELLING_PRICE_LIST}, fields=["*"]), corrected_list)
            self.assertEqual(frappe.get_all("Item Price", fields=["*"], order_by="name"), corrected_prices)

    def test_item_price_noop_keeps_the_entire_stored_row_unchanged(self) -> None:
        from kopos_connector.smoke import DEMO_DRINK_ITEM, SMOKE_SELLING_PRICE_LIST, _ensure_smoke_item_price

        frappe.set_user("Administrator")
        filters = {"item_code": DEMO_DRINK_ITEM, "price_list": SMOKE_SELLING_PRICE_LIST, "uom": "Nos"}
        before = frappe.get_all("Item Price", filters=filters, fields=["*"])
        self.assertEqual(len(before), 1)
        self.assertEqual(Decimal(str(before[0].price_list_rate)), Decimal("12"))
        for _ in range(2):
            self.assertEqual(_ensure_smoke_item_price(DEMO_DRINK_ITEM, "MYR"), before[0].name)
            self.assertEqual(frappe.get_all("Item Price", filters=filters, fields=["*"]), before)

    def test_item_price_corrects_a_real_mismatch_once_then_stops_writing(self) -> None:
        from kopos_connector.smoke import DEMO_DRINK_ITEM, SMOKE_SELLING_PRICE_LIST, _ensure_smoke_item_price

        frappe.set_user("Administrator")
        filters = {"item_code": DEMO_DRINK_ITEM, "price_list": SMOKE_SELLING_PRICE_LIST, "uom": "Nos"}
        name = frappe.db.get_value("Item Price", filters, "name")
        self.assertTrue(name)
        frappe.db.set_value("Item Price", name, "price_list_rate", Decimal("11.75"))
        self.assertEqual(_ensure_smoke_item_price(DEMO_DRINK_ITEM, "MYR"), name)
        corrected = frappe.get_all("Item Price", filters=filters, fields=["*"])
        self.assertEqual(len(corrected), 1)
        self.assertEqual(Decimal(str(corrected[0].price_list_rate)), Decimal("12"))
        for _ in range(2):
            self.assertEqual(_ensure_smoke_item_price(DEMO_DRINK_ITEM, "MYR"), name)
            self.assertEqual(frappe.get_all("Item Price", filters=filters, fields=["*"]), corrected)

    def test_pos_profile_noop_preserves_profile_payment_rows_and_device_versions(self) -> None:
        from kopos_connector.smoke import _ensure_pos_profile

        frappe.set_user("Administrator")
        profile = frappe.get_doc("POS Profile", "KoPOS Main")
        tables = ("POS Profile", "POS Payment Method", "KoPOS Device")
        before = {table: frappe.get_all(table, fields=["*"], order_by="name") for table in tables}
        for _ in range(2):
            self.assertEqual(_ensure_pos_profile(
                profile.company, profile.warehouse, profile.customer,
                profile.write_off_account, profile.write_off_cost_center,
            ), profile.name)
            self.assertEqual({table: frappe.get_all(table, fields=["*"], order_by="name")
                              for table in tables}, before)

    def test_pos_profile_corrects_once_then_stops_writing(self) -> None:
        from kopos_connector.smoke import _ensure_pos_profile

        frappe.set_user("Administrator")
        profile = frappe.get_doc("POS Profile", "KoPOS Main")
        profile.write_off_limit = 1
        profile.save(ignore_permissions=True)
        self.assertEqual(_ensure_pos_profile(
            profile.company, profile.warehouse, profile.customer,
            profile.write_off_account, profile.write_off_cost_center,
        ), profile.name)
        profile.reload()
        self.assertEqual(profile.write_off_limit, 0)
        before = frappe.get_all("POS Profile", fields=["*"], order_by="name")
        for _ in range(2):
            _ensure_pos_profile(profile.company, profile.warehouse, profile.customer,
                                profile.write_off_account, profile.write_off_cost_center)
            self.assertEqual(frappe.get_all("POS Profile", fields=["*"], order_by="name"), before)

    def test_no_transactional_test_business_records_were_committed(self) -> None:
        leaked_shifts = frappe.get_all(
            "FB Shift",
            filters={"shift_code": ("like", "KOPOS-%")},
            pluck="name",
        )
        leaked_orders = frappe.get_all(
            "FB Order",
            filters={"external_idempotency_key": ("like", "KOPOS-%")},
            pluck="name",
        )

        self.assertEqual(leaked_shifts, [])
        self.assertEqual(leaked_orders, [])
