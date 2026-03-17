import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from .fake_frappe import install_fake_frappe_modules


install_fake_frappe_modules()

from kopos_connector.api.orders import (
    build_refund_promotion_allocation,
    determine_reconciliation_status,
)
from kopos_connector.api.promotions import (
    classify_reconciliation_severity,
    derive_review_status,
    reconcile_promotion_payload,
    serialize_promotion,
)


class PromotionWorkflowTests(unittest.TestCase):
    def test_determine_reconciliation_status_covers_online_and_offline_modes(self):
        self.assertEqual(
            determine_reconciliation_status({"applied_promotions": []}),
            "not_applicable",
        )
        self.assertEqual(
            determine_reconciliation_status(
                {
                    "applied_promotions": [{"promotion_id": "PROMO-1"}],
                    "offline_priced": False,
                    "pricing_context": {},
                }
            ),
            "matched",
        )
        self.assertEqual(
            determine_reconciliation_status(
                {
                    "applied_promotions": [{"promotion_id": "PROMO-1"}],
                    "offline_priced": True,
                    "pricing_context": {},
                }
            ),
            "pending",
        )
        self.assertEqual(
            determine_reconciliation_status(
                {
                    "applied_promotions": [{"promotion_id": "PROMO-1"}],
                    "offline_priced": True,
                    "pricing_context": {"restricted_mode": True},
                }
            ),
            "review_required",
        )

    def test_get_promotion_snapshot_payload_returns_none_when_no_published_snapshot(
        self,
    ):
        from kopos_connector.api.promotions import get_promotion_snapshot_payload

        with patch(
            "kopos_connector.api.promotions.get_latest_published_snapshot",
            return_value=None,
        ):
            with patch(
                "kopos_connector.api.promotions.resolve_snapshot_pos_profile",
                return_value="Test POS",
            ):
                result = get_promotion_snapshot_payload(pos_profile="Test POS")

        self.assertIsNone(result)

    def test_snapshot_deletion_blocked_when_referenced_by_invoice(self):
        import frappe
        from kopos_connector.kopos.doctype.kopos_promotion_snapshot.kopos_promotion_snapshot import (
            KoPOSPromotionSnapshot,
        )

        snapshot = KoPOSPromotionSnapshot()
        snapshot.snapshot_version = "KOPOS-PROMO-20260317000000-ABCD1234"

        with patch.object(frappe.db, "exists", return_value="POS-INV-001"):
            with self.assertRaises(frappe.ValidationError):
                snapshot.on_trash()

    def test_reconcile_promotion_payload_matches_exact_snapshot_version(self):
        snapshot = SimpleNamespace(
            snapshot_hash="hash-1",
            snapshot_version="snap-1",
            snapshot_payload=json.dumps(
                {"promotions": [{"promotion_id": "PROMO-1"}]},
                sort_keys=True,
            ),
        )
        payload = {
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

        with patch(
            "kopos_connector.api.promotions.get_snapshot_by_version",
            return_value=snapshot,
        ):
            result = reconcile_promotion_payload("KoPOS Main", payload)

        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["snapshot_version"], "snap-1")
        self.assertEqual(result["snapshot_hash"], "hash-1")
        self.assertEqual(
            classify_reconciliation_severity(result["status"], result["message"]),
            "none",
        )
        self.assertEqual(derive_review_status(result["status"]), "not_required")

    def test_reconcile_promotion_payload_flags_major_review_for_missing_snapshot(self):
        payload = {
            "pricing_context": {
                "snapshot_version": "missing",
                "snapshot_hash": "hash-1",
            },
            "applied_promotions": [{"promotion_id": "PROMO-1", "amount": 4.5}],
            "order": {"items": [{"promotion_allocations": [{"amount": 4.5}]}]},
        }

        with patch(
            "kopos_connector.api.promotions.get_snapshot_by_version",
            return_value=None,
        ):
            result = reconcile_promotion_payload("KoPOS Main", payload)

        self.assertEqual(result["status"], "review_required")
        self.assertEqual(result["severity"], "major")
        self.assertEqual(result["review_route"], "manager_review")
        self.assertEqual(derive_review_status(result["status"]), "pending_review")

    def test_reconcile_promotion_payload_flags_minor_review_for_amount_mismatch(self):
        snapshot = SimpleNamespace(
            snapshot_hash="hash-1",
            snapshot_version="snap-1",
            snapshot_payload=json.dumps(
                {"promotions": [{"promotion_id": "PROMO-1"}]},
                sort_keys=True,
            ),
        )
        payload = {
            "pricing_context": {
                "snapshot_version": "snap-1",
                "snapshot_hash": "hash-1",
            },
            "applied_promotions": [{"promotion_id": "PROMO-1", "amount": 4.5}],
            "order": {
                "items": [
                    {
                        "promotion_allocations": [
                            {"promotion_id": "PROMO-1", "amount": 3.0}
                        ]
                    }
                ]
            },
        }

        with patch(
            "kopos_connector.api.promotions.get_snapshot_by_version",
            return_value=snapshot,
        ):
            result = reconcile_promotion_payload("KoPOS Main", payload)

        self.assertEqual(result["status"], "review_required")
        self.assertEqual(result["severity"], "minor")
        self.assertEqual(result["review_route"], "ops_review")

    def test_partial_refund_allocation_scales_original_promotion_evidence(self):
        original_item = SimpleNamespace(
            qty=2,
            amount=25.5,
            rate=12.75,
            custom_kopos_promotion_allocation=json.dumps(
                {
                    "base_amount": 30.0,
                    "discount_amount": 4.5,
                    "promotion_allocations": [
                        {
                            "promotion_id": "Second Cup 30% Off",
                            "amount": 4.5,
                            "quantity": 1.0,
                            "scope": "line",
                        }
                    ],
                },
                sort_keys=True,
            ),
        )

        result = json.loads(build_refund_promotion_allocation(original_item, 1) or "{}")

        self.assertEqual(result["refund_context"]["refund_qty"], 1.0)
        self.assertEqual(result["refund_context"]["refunded_base_amount"], 15.0)
        self.assertEqual(result["refund_context"]["refunded_discount_amount"], 2.25)
        self.assertEqual(
            result["refund_context"]["refunded_promotion_allocations"][0]["amount"],
            2.25,
        )

    def test_serialize_promotion_includes_activation_mode_for_pos_presets(self):
        promo = SimpleNamespace(
            name="PROMO-STAFF-10",
            promotion_name="Staff Discount",
            display_label="Staff Discount",
            customer_message="10% off the order total.",
            promotion_type="order_discount",
            activation_mode="manual_selectable",
            offline_allowed=1,
            priority=20,
            stacking_policy="exclusive",
            discount_target="cheaper_eligible",
            discount_type="percentage",
            discount_value=10,
            buy_qty=1,
            discount_qty=1,
            repeat_mode="once",
            eligible_scope_mode="eligible_pool",
            comparison_basis="base_item_only",
            discount_basis="base_item_only",
            modifier_policy="excluded_by_default",
            valid_from=None,
            valid_upto=None,
            eligible_items=[],
            min_qty=0,
            min_amount=0,
            eligible_item_groups=[],
            eligible_pos_profiles=[],
        )

        payload = serialize_promotion(promo, "KoPOS Main")

        self.assertEqual(payload["activation_mode"], "manual_selectable")
        self.assertEqual(payload["display_label"], "Staff Discount")


if __name__ == "__main__":
    unittest.main()
