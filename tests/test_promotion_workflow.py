import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from .fake_frappe import install_fake_frappe_modules


install_fake_frappe_modules()

import frappe

from kopos_connector.api.orders import (
    build_refund_promotion_allocation,
    determine_reconciliation_status,
)
from kopos_connector.api.promotions import (
    build_effective_snapshot_body,
    build_snapshot_version_from_hash,
    classify_reconciliation_severity,
    compute_snapshot_content_hash,
    derive_review_status,
    get_promotion_snapshot_payload,
    publish_promotion_snapshot,
    reconcile_promotion_payload,
    repair_promotion_reconciliation_invoices,
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

    def test_build_snapshot_version_from_hash_is_deterministic(self):
        snapshot_hash = "abcdef1234567890fedcba0987654321"
        self.assertEqual(
            build_snapshot_version_from_hash(snapshot_hash),
            "KOPOS-PROMO-ABCDEF1234567890",
        )

    def test_effective_snapshot_hash_is_stable_when_child_rows_are_reordered(self):
        promotion_a = SimpleNamespace(
            name="PROMO-1",
            promotion_name="Promo",
            display_label="Promo",
            customer_message="",
            promotion_type="order_discount",
            activation_mode="automatic",
            offline_allowed=1,
            priority=10,
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
            eligible_items=[
                SimpleNamespace(item_code="ITEM-B"),
                SimpleNamespace(item_code="ITEM-A"),
            ],
            min_qty=0,
            min_amount=0,
            eligible_item_groups=[
                SimpleNamespace(item_group="GROUP-B"),
                SimpleNamespace(item_group="GROUP-A"),
            ],
            eligible_pos_profiles=[
                SimpleNamespace(pos_profile="POS-B"),
                SimpleNamespace(pos_profile="POS-A"),
            ],
        )
        promotion_b = SimpleNamespace(
            **{
                **promotion_a.__dict__,
                "eligible_items": [
                    SimpleNamespace(item_code="ITEM-A"),
                    SimpleNamespace(item_code="ITEM-B"),
                ],
                "eligible_item_groups": [
                    SimpleNamespace(item_group="GROUP-A"),
                    SimpleNamespace(item_group="GROUP-B"),
                ],
                "eligible_pos_profiles": [
                    SimpleNamespace(pos_profile="POS-A"),
                    SimpleNamespace(pos_profile="POS-B"),
                ],
            }
        )

        with patch(
            "kopos_connector.api.promotions.get_active_promotions",
            return_value=[promotion_a],
        ):
            hash_a = compute_snapshot_content_hash(
                build_effective_snapshot_body("KoPOS Main")
            )
        with patch(
            "kopos_connector.api.promotions.get_active_promotions",
            return_value=[promotion_b],
        ):
            hash_b = compute_snapshot_content_hash(
                build_effective_snapshot_body("KoPOS Main")
            )

        self.assertEqual(hash_a, hash_b)

    def test_get_promotion_snapshot_payload_returns_live_snapshot_when_no_published(
        self,
    ):
        live_payload = {
            "pos_profile": "Test POS",
            "effective_from": "2026-03-13T18:05:00",
            "promotions": [{"promotion_id": "PROMO-1"}],
            "published_at": "2026-03-13T18:05:00",
            "snapshot_hash": "live-hash",
            "snapshot_version": "KOPOS-PROMO-live-hash",
        }
        with (
            patch(
                "kopos_connector.api.promotions.get_latest_published_snapshot",
                return_value=None,
            ),
            patch(
                "kopos_connector.api.promotions.resolve_snapshot_pos_profile",
                return_value="Test POS",
            ),
            patch(
                "kopos_connector.api.promotions.build_snapshot_payload",
                return_value=live_payload,
            ),
        ):
            result = get_promotion_snapshot_payload(pos_profile="Test POS")

        self.assertIsNotNone(result)
        self.assertEqual(result["source"], "live")
        self.assertFalse(result["is_current"])

    def test_get_promotion_snapshot_payload_returns_latest_persisted_snapshot_only(
        self,
    ):
        latest = SimpleNamespace(
            snapshot_payload=json.dumps({"promotions": [{"promotion_id": "PROMO-1"}]}),
            snapshot_version="snap-1",
            snapshot_hash="hash-1",
            published_at="2026-03-13T18:05:00",
            effective_from="2026-03-13T18:05:00",
            pos_profile="KoPOS Main",
        )
        with (
            patch(
                "kopos_connector.api.promotions.resolve_snapshot_pos_profile",
                return_value="KoPOS Main",
            ),
            patch(
                "kopos_connector.api.promotions.get_latest_published_snapshot",
                return_value=latest,
            ),
            patch(
                "kopos_connector.api.promotions.ensure_persisted_snapshot",
                side_effect=AssertionError("fetch must be read-only"),
            ),
        ):
            result = get_promotion_snapshot_payload(
                pos_profile="KoPOS Main",
                current_version="snap-1",
            )

        self.assertEqual(result["snapshot_version"], "snap-1")
        self.assertEqual(result["snapshot_hash"], "hash-1")
        self.assertEqual(result["source"], "published")
        self.assertTrue(result["is_current"])

    def test_publish_promotion_snapshot_returns_unchanged_when_latest_hash_matches(
        self,
    ):
        latest = SimpleNamespace(
            snapshot_version="snap-1",
            snapshot_hash="hash-1",
            pos_profile="KoPOS Main",
            promotion_count=1,
        )
        with (
            patch(
                "kopos_connector.api.promotions.require_system_manager",
            ),
            patch(
                "kopos_connector.api.promotions.resolve_snapshot_pos_profile",
                return_value="KoPOS Main",
            ),
            patch(
                "kopos_connector.api.promotions.build_snapshot_payload_for_persistence",
                return_value={
                    "promotions": [{"promotion_id": "PROMO-1"}],
                    "snapshot_hash": "hash-1",
                },
            ),
            patch(
                "kopos_connector.api.promotions.get_latest_published_snapshot",
                return_value=latest,
            ),
            patch(
                "kopos_connector.api.promotions.ensure_persisted_snapshot",
                side_effect=AssertionError("unchanged publish should not persist"),
            ),
        ):
            result = publish_promotion_snapshot(pos_profile="KoPOS Main")

        self.assertEqual(result["status"], "unchanged")
        self.assertEqual(result["snapshot_version"], "snap-1")
        self.assertEqual(result["snapshot_hash"], "hash-1")

    def test_publish_promotion_snapshot_reuses_single_evaluated_payload(self):
        payload = {
            "promotions": [{"promotion_id": "PROMO-1"}],
            "snapshot_hash": "hash-1",
            "snapshot_version": "snap-1",
            "published_at": "2026-03-13T18:05:00",
            "effective_from": "2026-03-13T18:05:00",
        }
        snapshot = SimpleNamespace(
            name="SNAP-1",
            snapshot_version="snap-1",
            snapshot_hash="hash-1",
            pos_profile="KoPOS Main",
            promotion_count=1,
            status="Published",
        )

        with (
            patch(
                "kopos_connector.api.promotions.require_system_manager",
            ),
            patch(
                "kopos_connector.api.promotions.resolve_snapshot_pos_profile",
                return_value="KoPOS Main",
            ),
            patch(
                "kopos_connector.api.promotions.build_snapshot_payload_for_persistence",
                return_value=payload,
            ) as build_payload,
            patch(
                "kopos_connector.api.promotions.get_latest_published_snapshot",
                return_value=None,
            ),
            patch(
                "kopos_connector.api.promotions.ensure_persisted_snapshot",
                return_value=(snapshot, True),
            ) as ensure_snapshot,
        ):
            result = publish_promotion_snapshot(pos_profile="KoPOS Main")

        build_payload.assert_called_once_with("KoPOS Main")
        ensure_snapshot.assert_called_once_with("KoPOS Main", payload=payload)
        self.assertEqual(result["status"], "published")
        self.assertEqual(result["snapshot_version"], "snap-1")

    def test_publish_promotion_snapshot_persists_empty_snapshot(self):
        payload = {
            "promotions": [],
            "snapshot_hash": "empty-hash",
            "snapshot_version": "empty-snap-1",
            "published_at": "2026-03-13T18:05:00",
            "effective_from": "2026-03-13T18:05:00",
        }
        snapshot = SimpleNamespace(
            name="SNAP-EMPTY-1",
            snapshot_version="empty-snap-1",
            snapshot_hash="empty-hash",
            pos_profile="KoPOS Main",
            promotion_count=0,
            status="Published",
        )

        with (
            patch(
                "kopos_connector.api.promotions.require_system_manager",
            ),
            patch(
                "kopos_connector.api.promotions.resolve_snapshot_pos_profile",
                return_value="KoPOS Main",
            ),
            patch(
                "kopos_connector.api.promotions.build_snapshot_payload_for_persistence",
                return_value=payload,
            ),
            patch(
                "kopos_connector.api.promotions.get_latest_published_snapshot",
                return_value=None,
            ),
            patch(
                "kopos_connector.api.promotions.ensure_persisted_snapshot",
                return_value=(snapshot, True),
            ) as ensure_snapshot,
        ):
            result = publish_promotion_snapshot(pos_profile="KoPOS Main")

        ensure_snapshot.assert_called_once_with("KoPOS Main", payload=payload)
        self.assertEqual(result["status"], "published")
        self.assertEqual(result["promotion_count"], 0)
        self.assertEqual(result["snapshot_version"], "empty-snap-1")

    def test_snapshot_deletion_blocked_when_referenced_by_invoice(self):
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

    def test_reconcile_promotion_payload_matches_hash_when_version_missing(self):
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
            "kopos_connector.api.promotions.get_snapshot_by_hash",
            return_value=snapshot,
        ):
            result = reconcile_promotion_payload("KoPOS Main", payload)

        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["snapshot_version"], "snap-1")
        self.assertEqual(result["snapshot_hash"], "hash-1")

    def test_reconcile_promotion_payload_flags_profile_mismatch_by_hash(self):
        snapshot = SimpleNamespace(
            snapshot_hash="hash-1",
            snapshot_version="snap-other",
            pos_profile="KoPOS Other",
        )
        payload = {
            "pricing_context": {
                "snapshot_hash": "hash-1",
            },
            "applied_promotions": [{"promotion_id": "PROMO-1", "amount": 4.5}],
            "order": {
                "items": [{"promotion_allocations": [{"amount": 4.5}]}],
            },
        }

        with (
            patch(
                "kopos_connector.api.promotions.get_snapshot_by_hash",
                return_value=None,
            ),
            patch(
                "kopos_connector.api.promotions.get_snapshot_by_hash_any_profile",
                return_value=snapshot,
            ),
        ):
            result = reconcile_promotion_payload("KoPOS Main", payload)

        self.assertEqual(result["status"], "review_required")
        self.assertEqual(result["message"], "Promotion snapshot profile mismatch")
        self.assertEqual(result["severity"], "major")

    def test_reconcile_promotion_payload_flags_missing_snapshot_metadata(self):
        payload = {
            "pricing_context": {},
            "applied_promotions": [{"promotion_id": "PROMO-1", "amount": 4.5}],
            "order": {
                "items": [{"promotion_allocations": [{"amount": 4.5}]}],
            },
        }

        result = reconcile_promotion_payload("KoPOS Main", payload)

        self.assertEqual(result["status"], "review_required")
        self.assertEqual(
            result["message"], "Applied promotions missing snapshot version"
        )
        self.assertEqual(result["severity"], "major")

    def test_reconcile_promotion_payload_flags_major_review_for_missing_snapshot(self):
        payload = {
            "pricing_context": {
                "snapshot_version": "missing",
                "snapshot_hash": "hash-1",
            },
            "applied_promotions": [{"promotion_id": "PROMO-1", "amount": 4.5}],
            "order": {"items": [{"promotion_allocations": [{"amount": 4.5}]}]},
        }

        with (
            patch(
                "kopos_connector.api.promotions.get_snapshot_by_version",
                return_value=None,
            ),
            patch(
                "kopos_connector.api.promotions.get_snapshot_by_hash",
                return_value=None,
            ),
            patch(
                "kopos_connector.api.promotions.get_snapshot_by_version_any_profile",
                return_value=None,
            ),
            patch(
                "kopos_connector.api.promotions.get_snapshot_by_hash_any_profile",
                return_value=None,
            ),
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

    def test_reconcile_promotion_payload_detects_cent_level_mismatch(self):
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
            "applied_promotions": [{"promotion_id": "PROMO-1", "amount": "4.50"}],
            "order": {
                "items": [
                    {
                        "promotion_allocations": [
                            {"promotion_id": "PROMO-1", "amount": "4.49"}
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
        self.assertEqual(
            result["message"],
            "Applied promotion total does not match line allocations",
        )

    def test_reconcile_promotion_payload_flags_invalid_monetary_evidence(self):
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
            "applied_promotions": [{"promotion_id": "PROMO-1", "amount": "bad"}],
            "order": {
                "items": [
                    {
                        "promotion_allocations": [
                            {"promotion_id": "PROMO-1", "amount": "4.50"}
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
        self.assertEqual(
            result["message"],
            "Applied promotion amount evidence is invalid",
        )
        self.assertEqual(result["severity"], "major")

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

    def test_repair_promotion_reconciliation_invoices_repairs_hash_only_invoice(self):
        invoice_row = {
            "name": "POS-INV-001",
            "pos_profile": "KoPOS Main",
            "custom_kopos_promotion_payload": json.dumps(
                {
                    "pricing_context": {"snapshot_hash": "hash-1"},
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
                },
                sort_keys=True,
            ),
        }
        invoice_doc = SimpleNamespace(
            name="POS-INV-001",
            custom_kopos_promotion_payload=invoice_row[
                "custom_kopos_promotion_payload"
            ],
            custom_kopos_promotion_reconciliation_status="review_required",
            custom_kopos_promotion_review_status="pending_review",
            custom_kopos_promotion_snapshot_version=None,
        )
        snapshot = SimpleNamespace(
            snapshot_hash="hash-1",
            snapshot_version="snap-1",
            snapshot_payload=json.dumps(
                {"promotions": [{"promotion_id": "PROMO-1"}]},
                sort_keys=True,
            ),
        )
        recorded_updates: list[dict[str, object]] = []

        def capture_update(_doctype, _name, values, update_modified=True):
            self.assertTrue(update_modified)
            recorded_updates.append(values)

        with (
            patch(
                "kopos_connector.api.promotions.require_system_manager",
            ),
            patch(
                "frappe.get_all",
                return_value=[invoice_row],
            ),
            patch(
                "frappe.get_doc",
                return_value=invoice_doc,
            ),
            patch(
                "frappe.db.set_value",
                side_effect=capture_update,
            ),
            patch(
                "kopos_connector.api.promotions.get_snapshot_by_hash",
                return_value=snapshot,
            ),
            patch(
                "kopos_connector.api.promotions.get_snapshot_by_hash_any_profile",
                return_value=None,
            ),
        ):
            result = repair_promotion_reconciliation_invoices(limit=1)

        self.assertEqual(result, {"scanned": 1, "repaired": 1, "still_pending": 0})
        self.assertEqual(len(recorded_updates), 1)
        self.assertEqual(
            recorded_updates[0]["custom_kopos_promotion_reconciliation_status"],
            "matched",
        )
        self.assertEqual(
            recorded_updates[0]["custom_kopos_promotion_review_status"],
            "not_required",
        )
        self.assertEqual(
            recorded_updates[0]["custom_kopos_promotion_snapshot_version"],
            "snap-1",
        )

    def test_repair_promotion_reconciliation_invoices_skips_already_reviewed_invoice(
        self,
    ):
        invoice_row = {
            "name": "POS-INV-REVIEWED",
            "pos_profile": "KoPOS Main",
            "custom_kopos_promotion_payload": json.dumps(
                {
                    "pricing_context": {"snapshot_hash": "hash-1"},
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
                },
                sort_keys=True,
            ),
            "custom_kopos_promotion_review_status": "approved_override",
        }

        with (
            patch(
                "kopos_connector.api.promotions.require_system_manager",
            ),
            patch(
                "frappe.get_all",
                return_value=[invoice_row],
            ),
            patch(
                "frappe.db.set_value",
                side_effect=AssertionError(
                    "reviewed invoice must not be auto-repaired"
                ),
            ),
        ):
            result = repair_promotion_reconciliation_invoices(limit=1)

        self.assertEqual(result, {"scanned": 1, "repaired": 0, "still_pending": 1})

    def test_repair_promotion_reconciliation_invoices_continues_after_bad_invoice(
        self,
    ):
        broken_invoice_row = {
            "name": "POS-INV-BAD",
            "pos_profile": "KoPOS Main",
            "custom_kopos_promotion_payload": "{",
            "custom_kopos_promotion_review_status": "pending_review",
        }
        good_invoice_row = {
            "name": "POS-INV-GOOD",
            "pos_profile": "KoPOS Main",
            "custom_kopos_promotion_payload": json.dumps(
                {
                    "pricing_context": {"snapshot_hash": "hash-1"},
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
                },
                sort_keys=True,
            ),
            "custom_kopos_promotion_review_status": "pending_review",
        }
        invoice_doc = SimpleNamespace(
            name="POS-INV-GOOD",
            custom_kopos_promotion_payload=good_invoice_row[
                "custom_kopos_promotion_payload"
            ],
            custom_kopos_promotion_reconciliation_status="review_required",
            custom_kopos_promotion_review_status="pending_review",
            custom_kopos_promotion_snapshot_version=None,
        )
        snapshot = SimpleNamespace(
            snapshot_hash="hash-1",
            snapshot_version="snap-1",
            snapshot_payload=json.dumps(
                {"promotions": [{"promotion_id": "PROMO-1"}]},
                sort_keys=True,
            ),
        )
        recorded_updates: list[dict[str, object]] = []

        def get_doc_side_effect(_doctype, name):
            self.assertEqual(name, "POS-INV-GOOD")
            return invoice_doc

        def capture_update(_doctype, _name, values, update_modified=True):
            self.assertTrue(update_modified)
            recorded_updates.append(values)

        with (
            patch(
                "kopos_connector.api.promotions.require_system_manager",
            ),
            patch(
                "frappe.get_all",
                return_value=[broken_invoice_row, good_invoice_row],
            ),
            patch(
                "frappe.get_doc",
                side_effect=get_doc_side_effect,
            ),
            patch(
                "frappe.db.set_value",
                side_effect=capture_update,
            ),
            patch(
                "kopos_connector.api.promotions.get_snapshot_by_hash",
                return_value=snapshot,
            ),
            patch(
                "kopos_connector.api.promotions.get_snapshot_by_hash_any_profile",
                return_value=None,
            ),
        ):
            result = repair_promotion_reconciliation_invoices(limit=2)

        self.assertEqual(result, {"scanned": 2, "repaired": 1, "still_pending": 1})
        self.assertEqual(len(recorded_updates), 1)


if __name__ == "__main__":
    unittest.main()
