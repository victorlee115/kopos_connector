from __future__ import annotations

import unittest
from types import SimpleNamespace

from kopos_connector.tests.fake_frappe import install_fake_frappe_modules

install_fake_frappe_modules()

import frappe

from kopos_connector.kopos.services.accounting.return_invoice_service import (
    _copy_promotion_provenance,
)


class TestReturnInvoicePromotionProvenance(unittest.TestCase):
    def make_original_invoice(self):
        return SimpleNamespace(
            custom_kopos_pricing_mode="offline_snapshot",
            custom_kopos_promotion_snapshot_version="PROMO-SNAPSHOT-1",
            custom_kopos_promotion_snapshot_hash="b" * 64,
            custom_kopos_promotion_reconciliation_status="matched",
            custom_kopos_promotion_payload=(
                '{"applied_promotions":'
                '[{"promotion_id":"SMOKE-MANUAL-10-PCT"}]}'
            ),
            items=[
                SimpleNamespace(
                    name="SINV-ITEM-1",
                    custom_fb_order_line_ref="LINE-1",
                    custom_fb_resolved_sale="RESOLVED-1",
                    custom_kopos_promotion_allocation=(
                        '[{"promotion_id":"SMOKE-MANUAL-10-PCT",'
                        '"amount_sen":120}]'
                    ),
                ),
                SimpleNamespace(
                    name="SINV-ITEM-2",
                    custom_fb_order_line_ref="LINE-2",
                    custom_fb_resolved_sale="RESOLVED-2",
                    custom_kopos_promotion_allocation="[]",
                ),
            ],
        )

    def test_copies_exact_header_and_line_evidence_by_source_item_reference(self):
        original_invoice = self.make_original_invoice()
        return_invoice = SimpleNamespace(
            items=[
                SimpleNamespace(sales_invoice_item="SINV-ITEM-2"),
                SimpleNamespace(sales_invoice_item="SINV-ITEM-1"),
            ]
        )

        _copy_promotion_provenance(original_invoice, return_invoice)

        for fieldname in (
            "custom_kopos_pricing_mode",
            "custom_kopos_promotion_snapshot_version",
            "custom_kopos_promotion_snapshot_hash",
            "custom_kopos_promotion_reconciliation_status",
            "custom_kopos_promotion_payload",
        ):
            self.assertEqual(
                getattr(return_invoice, fieldname),
                getattr(original_invoice, fieldname),
            )
        self.assertEqual(
            return_invoice.items[0].custom_kopos_promotion_allocation,
            "[]",
        )
        self.assertEqual(
            return_invoice.items[1].custom_kopos_promotion_allocation,
            '[{"promotion_id":"SMOKE-MANUAL-10-PCT","amount_sen":120}]',
        )

    def test_copies_line_evidence_by_order_line_reference_when_no_copy_removed_it(self):
        original_invoice = self.make_original_invoice()
        return_invoice = SimpleNamespace(
            items=[
                SimpleNamespace(custom_fb_order_line_ref="LINE-1"),
                SimpleNamespace(custom_fb_order_line_ref="LINE-2"),
            ]
        )

        _copy_promotion_provenance(original_invoice, return_invoice)

        self.assertEqual(
            return_invoice.items[0].custom_kopos_promotion_allocation,
            original_invoice.items[0].custom_kopos_promotion_allocation,
        )
        self.assertEqual(
            return_invoice.items[1].custom_kopos_promotion_allocation,
            original_invoice.items[1].custom_kopos_promotion_allocation,
        )

    def test_rejects_return_item_without_unique_original_identity(self):
        original_invoice = self.make_original_invoice()
        return_invoice = SimpleNamespace(
            items=[
                SimpleNamespace(custom_fb_order_line_ref="UNKNOWN"),
                SimpleNamespace(custom_fb_order_line_ref="LINE-2"),
            ]
        )

        with self.assertRaisesRegex(
            frappe.ValidationError,
            "return item has no unique original Sales Invoice item",
        ):
            _copy_promotion_provenance(original_invoice, return_invoice)


if __name__ == "__main__":
    unittest.main()
