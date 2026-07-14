import importlib
import unittest
from unittest.mock import patch

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


if __name__ == "__main__":
    unittest.main()
