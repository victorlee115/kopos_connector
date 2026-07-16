from __future__ import annotations

import unittest
from unittest.mock import patch

from kopos_connector.tests.fake_frappe import install_fake_frappe_modules

install_fake_frappe_modules()

import frappe

from kopos_connector.kopos.doctype.kopos_promotion_snapshot import (
    kopos_promotion_snapshot,
)


class TestPromotionSnapshotDeletionGuards(unittest.TestCase):
    def make_snapshot(self):
        snapshot = kopos_promotion_snapshot.KoPOSPromotionSnapshot()
        snapshot.snapshot_version = "PROMO-SNAPSHOT-2026-07-15"
        snapshot.snapshot_hash = "a" * 64
        return snapshot

    def assert_reference_is_blocked(
        self,
        doctype: str,
        fieldname: str,
        reference_name: str,
        expected_reference_kind: str,
    ) -> None:
        calls: list[tuple[str, dict[str, str]]] = []

        def exists(candidate_doctype, filters):
            calls.append((candidate_doctype, filters))
            if candidate_doctype == doctype and fieldname in filters:
                return reference_name
            return False

        with patch.object(frappe.db, "exists", side_effect=exists):
            with self.assertRaisesRegex(
                frappe.ValidationError,
                rf"referenced by {doctype} {reference_name} via {expected_reference_kind}",
            ):
                self.make_snapshot().on_trash()

        self.assertNotIn("POS Invoice", [candidate[0] for candidate in calls])

    def test_fb_order_version_reference_blocks_deletion(self):
        self.assert_reference_is_blocked(
            "FB Order",
            "promotion_snapshot_version",
            "FB-ORDER-0001",
            "version",
        )

    def test_sales_invoice_version_reference_blocks_deletion(self):
        self.assert_reference_is_blocked(
            "Sales Invoice",
            "custom_kopos_promotion_snapshot_version",
            "SINV-0001",
            "version",
        )

    def test_fb_order_hash_reference_blocks_deletion(self):
        self.assert_reference_is_blocked(
            "FB Order",
            "promotion_snapshot_hash",
            "FB-ORDER-0002",
            "hash",
        )

    def test_sales_invoice_hash_reference_blocks_deletion(self):
        self.assert_reference_is_blocked(
            "Sales Invoice",
            "custom_kopos_promotion_snapshot_hash",
            "SINV-0002",
            "hash",
        )

    def test_unreferenced_snapshot_can_be_deleted(self):
        calls: list[tuple[str, dict[str, str]]] = []

        def exists(doctype, filters):
            calls.append((doctype, filters))
            return False

        with patch.object(frappe.db, "exists", side_effect=exists):
            self.make_snapshot().on_trash()

        self.assertEqual(
            calls,
            [
                (
                    "FB Order",
                    {"promotion_snapshot_version": "PROMO-SNAPSHOT-2026-07-15"},
                ),
                ("FB Order", {"promotion_snapshot_hash": "a" * 64}),
                (
                    "Sales Invoice",
                    {
                        "custom_kopos_promotion_snapshot_version": "PROMO-SNAPSHOT-2026-07-15"
                    },
                ),
                (
                    "Sales Invoice",
                    {"custom_kopos_promotion_snapshot_hash": "a" * 64},
                ),
            ],
        )


if __name__ == "__main__":
    unittest.main()
