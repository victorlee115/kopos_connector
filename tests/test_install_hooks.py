import importlib
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from .fake_frappe import install_fake_frappe_modules


install_fake_frappe_modules()
install_module = importlib.import_module("kopos_connector.install.install")
quarantine_legacy_modifier_report = importlib.import_module(
    "kopos_connector.patches.quarantine_legacy_modifier_report"
)


class InstallHookTests(unittest.TestCase):
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
                return ["director@example.test"]
            if doctype == "Has Role":
                return []
            raise AssertionError(f"unexpected DocType read: {doctype}")

        with (
            patch.object(install_module.frappe.db, "exists", side_effect=exists),
            patch.object(install_module.frappe.db, "set_value") as set_value,
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


if __name__ == "__main__":
    unittest.main()
