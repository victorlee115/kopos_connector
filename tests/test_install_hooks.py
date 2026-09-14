import importlib
import sys
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import Mock, patch

from .fake_frappe import install_fake_frappe_modules


install_fake_frappe_modules()
install_module = importlib.import_module("kopos_connector.install.install")
quarantine_legacy_modifier_report = importlib.import_module(
    "kopos_connector.patches.quarantine_legacy_modifier_report"
)


class InstallHookTests(unittest.TestCase):
    def test_provisioning_scripts_save_only_a_real_change(self):
        for ensure, doctype in (
            (install_module.ensure_kopos_device_provisioning_script, "KoPOS Device"),
            (install_module.ensure_pos_profile_provisioning_script, "POS Profile"),
        ):
            for field in (None, "dt", "view", "enabled", "script"):
                with self.subTest(doctype=doctype, field=field):
                    document = SimpleNamespace(save=Mock(), description="Retain custom description")
                    with patch.object(install_module.frappe.db, "exists", return_value=True), patch.object(
                        install_module.frappe, "get_doc", return_value=document
                    ), patch.object(install_module.frappe.db, "commit"):
                        ensure()
                        expected = {key: getattr(document, key) for key in ("dt", "view", "enabled", "script")}
                        self.assertEqual(expected["dt"], doctype)
                        document.save.reset_mock()
                        if field:
                            setattr(document, field, 0 if field == "enabled" else "outdated")
                        ensure()
                        ensure()
                    self.assertEqual(document.save.call_count, int(field is not None))
                    self.assertEqual({key: getattr(document, key) for key in expected}, expected)
                    self.assertEqual(document.description, "Retain custom description")

    def test_provisioning_scripts_insert_with_native_metadata(self):
        for ensure, name, doctype in (
            (install_module.ensure_kopos_device_provisioning_script, "KoPOS Device Provisioning Shortcut", "KoPOS Device"),
            (install_module.ensure_pos_profile_provisioning_script, "KoPOS POS Profile Provisioning Shortcut", "POS Profile"),
        ):
            with self.subTest(doctype=doctype):
                document = Mock()
                with patch.object(install_module.frappe.db, "exists", return_value=False), patch.object(
                    install_module.frappe, "get_doc", return_value=document
                ) as load, patch.object(install_module.frappe.db, "commit"):
                    ensure()
                values = load.call_args.args[0]
                self.assertEqual({key: values[key] for key in ("doctype", "name", "dt", "view", "enabled")},
                                 {"doctype": "Client Script", "name": name, "dt": doctype, "view": "Form", "enabled": 1})
                self.assertTrue(values["script"])
                document.insert.assert_called_once_with(ignore_permissions=True)
                document.save.assert_not_called()

    def test_profile_script_uses_the_single_packaged_javascript_asset(self):
        from importlib.resources import files
        document = SimpleNamespace(save=Mock())
        with patch.object(install_module.frappe.db, "exists", return_value=True), patch.object(
            install_module.frappe, "get_doc", return_value=document
        ), patch.object(install_module.frappe.db, "commit") as commit:
            install_module.ensure_pos_profile_provisioning_script()
        self.assertEqual(document.script, files("kopos_connector").joinpath("public/js/pos_profile_setup.js").read_text(encoding="utf-8"))
        self.assertEqual(document.dt, "POS Profile")
        self.assertEqual(document.enabled, 1)
        document.save.assert_called_once_with(ignore_permissions=True)
        commit.assert_called_once_with()

    def test_native_invoice_precision_changes_only_rate_fields_and_is_idempotent(self):
        precision = {"rate": 2, "net_rate": 5, "base_rate": 2, "base_net_rate": 2}

        def set_precision(doctype, fieldname, property_name, value, property_type):
            self.assertEqual((doctype, property_name, value, property_type),
                             ("Sales Invoice Item", "precision", "6", "Int"))
            precision[fieldname] = int(value)

        setter = Mock(side_effect=set_precision)
        module = SimpleNamespace(make_property_setter=setter)
        with patch.dict(sys.modules, {"frappe.custom.doctype.property_setter.property_setter": module}), patch.object(
            install_module.frappe, "get_precision", create=True,
            side_effect=lambda doctype, fieldname: precision[fieldname],
        ):
            install_module.ensure_sales_invoice_rate_precision()
            self.assertEqual(setter.call_count, 4)
            install_module.ensure_sales_invoice_rate_precision()
            self.assertEqual(setter.call_count, 4)

    def test_native_invoice_precision_preserves_equal_or_higher_site_settings(self):
        setter = Mock()
        module = SimpleNamespace(make_property_setter=setter)
        with patch.dict(sys.modules, {"frappe.custom.doctype.property_setter.property_setter": module}), patch.object(
            install_module.frappe, "get_precision", create=True, side_effect=[6, 7, 8, 9],
        ):
            install_module.ensure_sales_invoice_rate_precision()
        setter.assert_not_called()

    def test_install_and_migrate_ensure_native_invoice_precision(self):
        for hook in (install_module.after_install, install_module.after_migrate):
            with self.subTest(hook=hook.__name__), ExitStack() as stack:
                for name in ("ensure_kopos_module_defs", "ensure_standard_pages", "ensure_jiji_business_module_profile",
                             "ensure_kopos_custom_fields", "create_fb_custom_fields", "ensure_maybank_provider_device_id",
                             "ensure_operational_composite_indexes"):
                    stack.enter_context(patch.object(install_module, name))
                ensure = stack.enter_context(patch.object(install_module, "ensure_sales_invoice_rate_precision"))
                change = stack.enter_context(patch.object(install_module, "ensure_native_change_ledger_entries"))
                hook()
                ensure.assert_called_once_with()
                change.assert_called_once_with()

    def test_native_change_ledger_setting_is_enabled_once(self):
        for initial in (0, 1):
            with self.subTest(initial=initial):
                settings = SimpleNamespace(post_change_gl_entries=initial, save=Mock())
                with patch.object(install_module.frappe, "get_single", create=True, return_value=settings) as load:
                    install_module.ensure_native_change_ledger_entries()
                    install_module.ensure_native_change_ledger_entries()
                self.assertEqual(settings.post_change_gl_entries, 1)
                self.assertEqual(load.call_args.args, ("POS Settings",))
                self.assertEqual(settings.save.call_count, 1 - initial)
                if not initial:
                    settings.save.assert_called_once_with(ignore_permissions=True)

    def test_before_migrate_preflights_duplicate_device_api_users(self):
        with patch.object(
            install_module, "normalize_duplicate_device_api_users"
        ) as normalize:
            install_module.before_migrate()

        normalize.assert_called_once_with()

    def test_quarantine_patch_disables_existing_legacy_report(self):
        with (
            patch.object(
                quarantine_legacy_modifier_report.frappe.db,
                "exists",
                return_value=True,
            ),
            patch.object(
                quarantine_legacy_modifier_report.frappe.db, "set_value"
            ) as set_value,
            patch.object(
                quarantine_legacy_modifier_report.frappe.db, "delete"
            ) as delete,
        ):
            quarantine_legacy_modifier_report.execute()

        delete.assert_called_once_with(
            "Scheduled Job Type",
            {"method": "kopos_connector.api.modifiers.aggregate_modifier_stats"},
        )
        set_value.assert_called_once_with(
            "Report",
            "Modifier Sales Analytics",
            "disabled",
            1,
            update_modified=False,
        )

    def test_business_module_profile_does_not_resave_an_unchanged_profile(self):
        profile = Mock()
        profile.get.return_value = [
            SimpleNamespace(module="Accounts"),
            SimpleNamespace(module="Stock"),
        ]
        profile.is_new.return_value = False
        module_profile_meta = SimpleNamespace(
            get_field=lambda fieldname: SimpleNamespace(options="Block Module")
            if fieldname == "block_modules"
            else None
        )
        child_meta = SimpleNamespace(
            fields=[SimpleNamespace(fieldname="module", fieldtype="Link", options="Module Def")]
        )

        def exists(doctype, name=None):
            return doctype in {"DocType", "Module Profile"}

        def get_all(doctype, **_kwargs):
            if doctype == "Module Def":
                return ["Accounts", "KoPOS", "Stock"]
            if doctype == "Has Role":
                return []
            raise AssertionError(f"unexpected DocType read: {doctype}")

        with (
            patch.object(install_module.frappe.db, "exists", side_effect=exists),
            patch.object(install_module.frappe, "get_all", side_effect=get_all),
            patch.object(install_module.frappe, "get_doc", return_value=profile),
            patch.object(
                install_module.frappe,
                "get_meta",
                side_effect=lambda doctype: module_profile_meta
                if doctype == "Module Profile"
                else child_meta,
            ),
        ):
            install_module.ensure_jiji_business_module_profile()

        profile.set.assert_not_called()
        profile.save.assert_not_called()
        profile.insert.assert_not_called()

    def test_business_director_leaves_landing_to_the_role_aware_hook(self):
        profile = Mock()
        profile.get.return_value = [SimpleNamespace(module="Accounts")]
        profile.is_new.return_value = False
        module_profile_meta = SimpleNamespace(
            get_field=lambda fieldname: SimpleNamespace(options="Block Module")
            if fieldname == "block_modules"
            else None
        )
        child_meta = SimpleNamespace(
            fields=[SimpleNamespace(fieldname="module", fieldtype="Link", options="Module Def")]
        )

        def exists(doctype, name=None):
            return (doctype, name) in {
                ("DocType", "Module Profile"),
                ("Module Profile", "JiJi Business"),
                ("User", "director@example.test"),
            }

        def get_all(doctype, **kwargs):
            if doctype == "Module Def":
                return ["Accounts", "KoPOS"]
            if doctype == "Has Role" and kwargs["filters"]["role"] == "Company Director":
                return ["director@example.test", "Administrator", "mixed@example.test"]
            if doctype == "Has Role":
                return ["Administrator", "mixed@example.test"]
            raise AssertionError(f"unexpected DocType read: {doctype}")

        with (
            patch.object(install_module.frappe.db, "exists", side_effect=exists),
            patch.object(install_module.frappe.db, "set_value") as set_value,
            patch.object(install_module.frappe, "log_error") as warning,
            patch.object(install_module.frappe, "get_all", side_effect=get_all),
            patch.object(install_module.frappe, "get_doc", return_value=profile),
            patch.object(
                install_module.frappe,
                "get_meta",
                side_effect=lambda doctype: module_profile_meta
                if doctype == "Module Profile"
                else child_meta,
            ),
        ):
            install_module.ensure_jiji_business_module_profile()

        set_value.assert_called_once_with(
            "User",
            "director@example.test",
            {
                "module_profile": "JiJi Business",
                "default_workspace": None,
            },
            update_modified=False,
        )
        warning.assert_called_once_with(
            title="JiJi business and support accounts must be separated",
            message="mixed@example.test",
        )


if __name__ == "__main__":
    unittest.main()
