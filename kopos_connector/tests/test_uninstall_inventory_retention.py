from __future__ import annotations

from unittest import TestCase
from unittest.mock import patch

from kopos_connector.tests.fake_frappe import install_fake_frappe_modules


install_fake_frappe_modules()

from kopos_connector import uninstall


class InventoryUninstallRetentionTests(TestCase):
    def test_uninstall_never_deletes_protected_legacy_evidence_fields(self) -> None:
        deleted: list[str] = []

        def delete_doc(_doctype: str, name: str, **_kwargs: object) -> None:
            deleted.append(name)

        with (
            patch.object(uninstall.frappe, "delete_doc", side_effect=delete_doc),
            patch.object(uninstall.frappe.db, "commit"),
        ):
            retained = uninstall.remove_custom_fields()

        self.assertEqual(retained, set(uninstall.PROTECTED_LEGACY_FIELDS))
        self.assertTrue(deleted)
        self.assertTrue(uninstall.PROTECTED_LEGACY_FIELDS.isdisjoint(deleted))
