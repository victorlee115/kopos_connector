import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

from .fake_frappe import install_fake_frappe_modules

install_fake_frappe_modules()

import frappe

from kopos_connector.api.orders import set_invoice_promotion_metadata
from kopos_connector.api.promotions import (
    amount_to_sen,
    get_promotion_review_queue,
    review_promotion_reconciliation,
)


from kopos_connector.kopos.doctype.kopos_promotion_snapshot.kopos_promotion_snapshot import (
    KoPOSPromotionSnapshot,
)


class PromotionReviewWorkflowTests(unittest.TestCase):
    def test_set_invoice_promotion_metadata_uses_invoice_profile_not_payload(self):
        invoice = SimpleNamespace(
            name="POS-INV-001",
            pos_profile="KoPOS Main",
        )
        payload = {
            "pos_profile": "KoPOS Evil",
            "pricing_context": {
                "snapshot_version": "snap-1",
                "snapshot_hash": "hash-1",
            },
            "applied_promotions": [{"promotion_id": "PROMO-1", "amount": 4.5}],
            "order": {
                "items": [
                    {
                        "promotion_allocations": [
                            {"promotion_id": "PROMO-1", "amount": 4.5}
                        ]
                    }
                ]
            },
        }
        snapshot = SimpleNamespace(
            snapshot_hash="hash-1",
            snapshot_version="snap-1",
            snapshot_payload=json.dumps(
                {"promotions": [{"promotion_id": "PROMO-1"}]},
                sort_keys=True,
            ),
        )

        reconciliation_calls: list[tuple[object, ...]] = []

        def capture_reconciliation(pos_profile, rec_payload):
            reconciliation_calls.append((pos_profile, rec_payload))
            return {
                "status": "matched",
                "message": "Promotion payload matched published snapshot",
                "severity": "none",
                "snapshot_version": "snap-1",
                "snapshot_hash": "hash-1",
            }

        with patch(
            "kopos_connector.api.promotions.reconcile_promotion_payload",
            side_effect=capture_reconciliation,
        ):
            set_invoice_promotion_metadata(invoice, payload)

        self.assertEqual(len(reconciliation_calls), 1)
        self.assertEqual(reconciliation_calls[0][0], "KoPOS Main")
        self.assertNotEqual(reconciliation_calls[0][0], "KoPOS Evil")

    def test_get_promotion_review_queue_returns_pending_items(self):
        invoice_row = {
            "name": "POS-INV-001",
            "posting_date": "2026-03-01",
            "customer": "CUST-001",
            "custom_kopos_pricing_mode": "offline_snapshot",
            "custom_kopos_promotion_snapshot_version": "snap-1",
            "custom_kopos_promotion_payload": json.dumps(
                {
                    "reconciliation": {
                        "status": "review_required",
                        "message": "Referenced promotion snapshot was not found",
                        "severity": "major",
                        "review_route": "manager_review",
                    }
                },
                sort_keys=True,
            ),
            "custom_kopos_promotion_review_status": "pending_review",
        }

        with (
            patch(
                "kopos_connector.api.promotions.require_system_manager",
            ),
            patch(
                "frappe.db.has_column",
                return_value=True,
            ),
            patch(
                "frappe.get_all",
                return_value=[invoice_row],
            ),
        ):
            result = get_promotion_review_queue(limit=20)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["pos_invoice"], "POS-INV-001")
        self.assertEqual(result[0]["severity"], "major")
        self.assertEqual(result[0]["review_route"], "manager_review")
        self.assertEqual(result[0]["review_status"], "pending_review")

    def test_get_promotion_review_queue_skips_already_reviewed_items(self):
        pending_invoice = {
            "name": "POS-INV-PENDING",
            "posting_date": "2026-03-01",
            "customer": "CUST-001",
            "custom_kopos_pricing_mode": "offline_snapshot",
            "custom_kopos_promotion_snapshot_version": "snap-1",
            "custom_kopos_promotion_payload": json.dumps(
                {"reconciliation": {"status": "review_required", "message": "Test"}},
                sort_keys=True,
            ),
            "custom_kopos_promotion_review_status": "pending_review",
        }
        reviewed_invoice = {
            "name": "POS-INV-REVIEWED",
            "posting_date": "2026-03-01",
            "customer": "CUST-001",
            "custom_kopos_pricing_mode": "offline_snapshot",
            "custom_kopos_promotion_snapshot_version": "snap-1",
            "custom_kopos_promotion_payload": json.dumps(
                {"reconciliation": {"status": "review_required", "message": "Test"}},
                sort_keys=True,
            ),
            "custom_kopos_promotion_review_status": "approved_override",
        }

        with (
            patch(
                "kopos_connector.api.promotions.require_system_manager",
            ),
            patch(
                "frappe.db.has_column",
                return_value=True,
            ),
            patch(
                "frappe.get_all",
                return_value=[pending_invoice, reviewed_invoice],
            ),
        ):
            result = get_promotion_review_queue(limit=20)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["pos_invoice"], "POS-INV-PENDING")

    def test_review_promotion_reconciliation_approves_pending_invoice(self):
        invoice = SimpleNamespace(
            name="POS-INV-001",
            pos_profile="KoPOS Main",
            custom_kopos_promotion_reconciliation_status="review_required",
            custom_kopos_promotion_review_status="pending_review",
            custom_kopos_promotion_payload=json.dumps(
                {
                    "reconciliation": {
                        "status": "review_required",
                        "message": "Referenced promotion snapshot was not found",
                        "severity": "major",
                        "review_route": "manager_review",
                    },
                    "audit_events": [],
                },
                sort_keys=True,
            ),
            custom_kopos_promotion_review_decision=None,
            custom_kopos_promotion_reviewed_by=None,
            custom_kopos_promotion_reviewed_at=None,
            custom_kopos_promotion_review_notes=None,
            add_comment=MagicMock(),
        )
        recorded_updates: list[dict[str, object]] = []

        def capture_update(_doctype, _name, values, update_modified=True):
            recorded_updates.append(values)

        with (
            patch(
                "kopos_connector.api.promotions.require_system_manager",
            ),
            patch(
                "frappe.db.exists",
                return_value=True,
            ),
            patch(
                "frappe.get_doc",
                return_value=invoice,
            ),
            patch(
                "frappe.db.set_value",
                side_effect=capture_update,
            ),
        ):
            result = review_promotion_reconciliation(
                pos_invoice="POS-INV-001",
                decision="approved_override",
                notes="Manager approved after review",
            )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["decision"], "approved_override")
        self.assertEqual(len(recorded_updates), 1)
        self.assertIn("custom_kopos_promotion_review_status", recorded_updates[0])
        self.assertIn("custom_kopos_promotion_review_decision", recorded_updates[0])
        self.assertIn("custom_kopos_promotion_reviewed_by", recorded_updates[0])
        self.assertIn("custom_kopos_promotion_review_notes", recorded_updates[0])

    def test_review_promotion_reconciliation_rejects_invalid_decision(self):
        invoice = SimpleNamespace(
            name="POS-INV-001",
            pos_profile="KoPOS Main",
            custom_kopos_promotion_reconciliation_status="review_required",
        )

        with (
            patch(
                "kopos_connector.api.promotions.require_system_manager",
            ),
            patch(
                "frappe.db.exists",
                return_value=True,
            ),
            patch(
                "frappe.get_doc",
                return_value=invoice,
            ),
        ):
            with self.assertRaises(frappe.ValidationError) as context:
                review_promotion_reconciliation(
                    pos_invoice="POS-INV-001",
                    decision="invalid_decision",
                )

    def test_review_promotion_reconciliation_rejects_already_reviewed_invoice(self):
        invoice = SimpleNamespace(
            name="POS-INV-001",
            pos_profile="KoPOS Main",
            custom_kopos_promotion_reconciliation_status="review_required",
            custom_kopos_promotion_review_status="approved_override",
        )

        with (
            patch(
                "kopos_connector.api.promotions.require_system_manager",
            ),
            patch(
                "frappe.db.exists",
                return_value=True,
            ),
            patch(
                "frappe.get_doc",
                return_value=invoice,
            ),
        ):
            with self.assertRaises(frappe.ValidationError) as context:
                review_promotion_reconciliation(
                    pos_invoice="POS-INV-001",
                    decision="approved_override",
                )

    def test_review_promotion_reconciliation_rejects_missing_invoice(self):
        with (
            patch(
                "kopos_connector.api.promotions.require_system_manager",
            ),
            patch(
                "frappe.db.exists",
                return_value=False,
            ),
        ):
            with self.assertRaises(frappe.ValidationError):
                review_promotion_reconciliation(
                    pos_invoice="POS-INV-NOTFOUND",
                    decision="approved_override",
                )

    def test_snapshot_deletion_blocked_by_hash_only_reference(self):
        snapshot = KoPOSPromotionSnapshot()
        snapshot.snapshot_version = "KOPOS-PROMO-HASHONLY"
        snapshot.snapshot_hash = "hash-only-1234abcd"

        with (
            patch(
                "frappe.db.exists",
                return_value=False,
            ),
            patch(
                "frappe.db.escape",
                side_effect=lambda x: x,
            ),
            patch(
                "frappe.db.sql",
                return_value=[["POS-INV-HASH-REF"]],
            ),
        ):
            with self.assertRaises(frappe.ValidationError):
                snapshot.on_trash()

    def test_amount_to_sen_converts_valid_floats(self):
        self.assertEqual(amount_to_sen("4.50"), 450)
        self.assertEqual(amount_to_sen("4.49"), 449)
        self.assertEqual(amount_to_sen("0.005"), 1)
        self.assertEqual(amount_to_sen("0.004"), 0)
        self.assertEqual(amount_to_sen("1.999"), 200)
        self.assertEqual(amount_to_sen("-0.01"), -1)

    def test_amount_to_sen_returns_none_for_invalid_input(self):
        self.assertIsNone(amount_to_sen(""))
        self.assertIsNone(amount_to_sen(None))
        self.assertIsNone(amount_to_sen("not-a-number"))
        self.assertIsNone(amount_to_sen("abc"))


if __name__ == "__main__":
    unittest.main()
