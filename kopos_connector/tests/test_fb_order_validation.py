from __future__ import annotations

import json
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from kopos_connector.tests.fake_frappe import install_fake_frappe_modules

install_fake_frappe_modules()


class TestFBOrderModifierValidation(unittest.TestCase):
    def test_prepare_resolves_default_recipe_before_freezing_sale(self):
        from kopos_connector.kopos.api import fb_orders

        events = []
        line = SimpleNamespace(
            recipe=None,
            recipe_version=None,
            is_recipe_managed=0,
        )
        payment = SimpleNamespace(name="PAY-1", settlement_status=None)

        class FakePreparedOrder:
            def __init__(self):
                self.items = [line]
                self.payments = [payment]
                self.name = "FB-ORDER-1"
                self._inserted_recipe_identity = None

            def build_line_resolutions(self):
                events.append("resolve")
                line.recipe = "RECIPE-1"
                line.recipe_version = 3
                line.is_recipe_managed = 1
                return [{"line": line, "resolved_components": []}]

            def insert(self, ignore_permissions=False):
                self._inserted_recipe_identity = (
                    line.recipe,
                    line.recipe_version,
                    line.is_recipe_managed,
                )
                events.append("insert")
                return self

            def validate_stock_availability(self, resolutions):
                events.append("stock")

            def create_resolved_sales(self, resolutions):
                events.append("snapshot")

            def get(self, fieldname):
                return getattr(self, fieldname)

            def save(self, ignore_permissions=False):
                current_recipe_identity = (
                    line.recipe,
                    line.recipe_version,
                    line.is_recipe_managed,
                )
                if current_recipe_identity != self._inserted_recipe_identity:
                    raise AssertionError(
                        "recipe identity changed after the prepared sale was frozen"
                    )
                events.append("save")
                return self

        order = FakePreparedOrder()
        normalized = {
            "external_idempotency_key": "IDEMP-1",
            "accepted_sale_fingerprint": "f" * 64,
            "payments": [],
        }
        validated = {
            **normalized,
            "shift": "FB-SHIFT-1",
            "device_id": "DEVICE-1",
            "staff_id": "cashier@example.com",
        }

        with patch.object(
            fb_orders,
            "_normalize_submit_order_payload",
            return_value=normalized,
        ), patch.object(
            fb_orders,
            "_validate_automatic_qr_prepare_payment",
        ), patch.object(
            fb_orders,
            "_get_existing_fb_order_name",
            return_value=None,
        ), patch.object(
            fb_orders,
            "_validate_new_submit_order_state",
            return_value=validated,
        ), patch.object(
            fb_orders,
            "_validate_submit_shift",
        ), patch.object(
            fb_orders,
            "_build_fb_order",
            return_value=order,
        ), patch.object(
            fb_orders,
            "_build_automatic_qr_prepare_response",
            return_value={"status": "ok"},
        ):
            response = fb_orders.prepare_automatic_qr_sale_payload({})

        self.assertEqual(response, {"status": "ok"})
        self.assertEqual(
            events,
            ["resolve", "insert", "stock", "snapshot", "save"],
        )
        self.assertEqual(
            order._inserted_recipe_identity,
            ("RECIPE-1", 3, 1),
        )

    def test_fully_discounted_line_normalizes_to_zero_sen(self):
        from kopos_connector.kopos.api.fb_orders import _normalize_order_item

        normalized = _normalize_order_item(
            {
                "line_id": "LINE-FREE-1",
                "item_code": "ITEM-FREE-1",
                "qty": 1,
                "unit_price_sen": 1200,
                "modifier_total_sen": 0,
                "discount_amount_sen": 1200,
                "line_total_sen": 0,
                "promotion_allocations": [
                    {
                        "promotion_id": "PROMO-FREE-1",
                        "amount_sen": 1200,
                        "quantity": 1,
                        "scope": "line",
                    }
                ],
            },
            1,
            "sen_v1",
        )

        self.assertEqual(normalized["line_total_sen"], 0)
        self.assertEqual(normalized["discount_amount_sen"], 1200)
        self.assertEqual(
            normalized["promotion_allocations"][0]["amount_sen"],
            1200,
        )

    def test_negative_line_total_remains_invalid(self):
        from kopos_connector.kopos.api.fb_orders import _normalize_order_item

        with self.assertRaisesRegex(Exception, "line_total_sen must be 0 or greater"):
            _normalize_order_item(
                {
                    "line_id": "LINE-NEGATIVE-1",
                    "item_code": "ITEM-NEGATIVE-1",
                    "qty": 1,
                    "unit_price_sen": 1200,
                    "modifier_total_sen": 0,
                    "discount_amount_sen": 1201,
                    "line_total_sen": -1,
                },
                1,
                "sen_v1",
            )

    def test_percentage_discount_rounds_half_up_and_caps_at_free(self):
        from kopos_connector.kopos.api.fb_orders import _snapshot_unit_discount_sen

        self.assertEqual(
            _snapshot_unit_discount_sen(
                {
                    "promotion_id": "PROMO-HALF-UP",
                    "discount_type": "percentage",
                    "discount_value": 10,
                },
                5,
            ),
            1,
        )
        self.assertEqual(
            _snapshot_unit_discount_sen(
                {
                    "promotion_id": "PROMO-FREE",
                    "discount_type": "percentage",
                    "discount_value": 100,
                },
                1200,
            ),
            1200,
        )
        self.assertEqual(
            _snapshot_unit_discount_sen(
                {
                    "promotion_id": "PROMO-OVER-100",
                    "discount_type": "percentage",
                    "discount_value": 150,
                },
                1200,
            ),
            1200,
        )

    def _stateful_promotion_fixture(self):
        from kopos_connector.api.promotions import (
            build_snapshot_version_from_hash,
            compute_snapshot_content_hash,
        )

        rule = {
            "promotion_id": "SMOKE-MANUAL-10-PCT",
            "promotion_name": "SMOKE-MANUAL-10-PCT",
            "promotion_type": "item_discount",
            "activation_mode": "manual_selectable",
            "offline_allowed": True,
            "priority": 10,
            "stacking_policy": "exclusive",
            "discount_type": "percentage",
            "discount_value": 10,
            "valid_from": None,
            "valid_upto": None,
            "eligible_items": ["SMOKE-STRAWBERRY-001"],
            "eligible_item_groups": [],
            "selected_pos_profiles": ["KoPOS Smoke Profile"],
            "min_qty": 1,
            "min_amount": 0,
        }
        body = {
            "pos_profile": "KoPOS Smoke Profile",
            "promotions": [rule],
        }
        snapshot_hash = compute_snapshot_content_hash(body)
        snapshot_version = build_snapshot_version_from_hash(snapshot_hash)
        payload = {
            **body,
            "effective_from": "2026-07-12T08:59:00",
            "published_at": "2026-07-12T08:59:00",
            "snapshot_hash": snapshot_hash,
            "snapshot_version": snapshot_version,
        }
        snapshot = SimpleNamespace(
            snapshot_version=snapshot_version,
            snapshot_hash=snapshot_hash,
            pos_profile="KoPOS Smoke Profile",
            status="Published",
            promotion_count=1,
            snapshot_payload=json.dumps(payload, sort_keys=True, separators=(",", ":")),
        )
        context = {
            "snapshot_version": snapshot_version,
            "snapshot_hash": snapshot_hash,
            "snapshot_downloaded_at": "2026-07-12T09:00:00",
            "snapshot_published_at": "2026-07-12T08:59:00",
            "snapshot_effective_from": "2026-07-12T08:59:00",
            "pricing_mode": "online_snapshot",
            "restricted_mode": False,
            "priced_at": "2026-07-12T10:01:00",
            "offline_applied_promotion_ids": [],
            "promotion_expiry_by_id": {"SMOKE-MANUAL-10-PCT": None},
        }
        normalized = {
            "device_id": "SMOKE-TAB-A001",
            "sale_datetime": datetime(2026, 7, 12, 10, 1),
            "offline_priced": False,
            "pricing_context": context,
            "applied_promotions": [
                {
                    "promotion_id": "SMOKE-MANUAL-10-PCT",
                    "promotion_name": "SMOKE-MANUAL-10-PCT",
                    "promotion_type": "item_discount",
                    "amount_sen": 120,
                    "scope": "order",
                    "source": "snapshot",
                    "snapshot_version": snapshot_version,
                    "snapshot_hash": snapshot_hash,
                    "valid_from": None,
                    "valid_upto": None,
                    "offline_applied": False,
                }
            ],
            "promotion_reconciliation_status": "matched",
            "items": [
                {
                    "line_id": "LINE-1",
                    "item_code": "SMOKE-STRAWBERRY-001",
                    "qty": 1,
                    "unit_price_sen": 1200,
                    "discount_amount_sen": 120,
                    "promotion_allocations": [
                        {
                            "promotion_id": "SMOKE-MANUAL-10-PCT",
                            "amount_sen": 120,
                            "quantity": 1,
                            "scope": "line",
                        }
                    ],
                }
            ],
        }
        return normalized, snapshot, payload

    @patch("kopos_connector.api.promotions.resolve_snapshot_pos_profile")
    @patch("kopos_connector.api.promotions.get_snapshot_by_version")
    def test_stateful_promotion_validation_recalculates_exact_ten_percent_sale(
        self, mock_get_snapshot, mock_resolve_profile
    ):
        from kopos_connector.kopos.api.fb_orders import (
            _validate_published_promotion_snapshot,
        )

        normalized, snapshot, _payload = self._stateful_promotion_fixture()
        mock_resolve_profile.return_value = "KoPOS Smoke Profile"
        mock_get_snapshot.return_value = snapshot

        evidence = _validate_published_promotion_snapshot(normalized)

        self.assertEqual(evidence["reconciliation"], {
            "status": "matched",
            "source": "published_snapshot",
        })
        self.assertEqual(evidence["applied_promotions"][0]["amount_sen"], 120)

    @patch("kopos_connector.api.promotions.resolve_snapshot_pos_profile")
    @patch("kopos_connector.api.promotions.get_snapshot_by_version")
    def test_stateful_promotion_validation_compares_priced_at_as_same_instant(
        self, mock_get_snapshot, mock_resolve_profile
    ):
        from kopos_connector.kopos.api.fb_orders import (
            _validate_published_promotion_snapshot,
        )

        normalized, snapshot, _payload = self._stateful_promotion_fixture()
        normalized["pricing_context"]["priced_at"] = "2026-07-12T02:01:00Z"
        mock_resolve_profile.return_value = "KoPOS Smoke Profile"
        mock_get_snapshot.return_value = snapshot

        evidence = _validate_published_promotion_snapshot(normalized)

        self.assertEqual(evidence["reconciliation"]["status"], "matched")

    @patch("kopos_connector.api.promotions.resolve_snapshot_pos_profile")
    @patch("kopos_connector.api.promotions.get_snapshot_by_version")
    def test_stateful_promotion_validation_rejects_wrong_percentage_math(
        self, mock_get_snapshot, mock_resolve_profile
    ):
        from kopos_connector.kopos.api.fb_orders import (
            _validate_published_promotion_snapshot,
        )

        normalized, snapshot, _payload = self._stateful_promotion_fixture()
        normalized["items"][0]["unit_price_sen"] = 1620
        mock_resolve_profile.return_value = "KoPOS Smoke Profile"
        mock_get_snapshot.return_value = snapshot

        with self.assertRaisesRegex(Exception, "server-recalculated snapshot pricing"):
            _validate_published_promotion_snapshot(normalized)

    @patch("kopos_connector.api.promotions.resolve_snapshot_pos_profile")
    @patch("kopos_connector.api.promotions.get_snapshot_by_version")
    def test_stateful_promotion_validation_recomputes_snapshot_content_hash(
        self, mock_get_snapshot, mock_resolve_profile
    ):
        from kopos_connector.kopos.api.fb_orders import (
            _validate_published_promotion_snapshot,
        )

        normalized, snapshot, payload = self._stateful_promotion_fixture()
        payload["promotions"][0]["discount_value"] = 99
        snapshot.snapshot_payload = json.dumps(
            payload, sort_keys=True, separators=(",", ":")
        )
        mock_resolve_profile.return_value = "KoPOS Smoke Profile"
        mock_get_snapshot.return_value = snapshot

        with self.assertRaisesRegex(Exception, "persisted identity is inconsistent"):
            _validate_published_promotion_snapshot(normalized)

    @patch("kopos_connector.api.promotions.resolve_snapshot_pos_profile")
    @patch("kopos_connector.api.promotions.get_snapshot_by_version")
    def test_snapshot_priced_sale_without_promotion_keeps_authoritative_context(
        self, mock_get_snapshot, mock_resolve_profile
    ):
        from kopos_connector.kopos.api.fb_orders import (
            _validate_published_promotion_snapshot,
        )

        normalized, snapshot, _payload = self._stateful_promotion_fixture()
        normalized["applied_promotions"] = []
        normalized["pricing_context"]["promotion_expiry_by_id"] = {}
        normalized["promotion_reconciliation_status"] = "not_applicable"
        normalized["items"][0]["discount_amount_sen"] = 0
        normalized["items"][0]["promotion_allocations"] = []
        mock_resolve_profile.return_value = "KoPOS Smoke Profile"
        mock_get_snapshot.return_value = snapshot

        evidence = _validate_published_promotion_snapshot(normalized)

        self.assertEqual(evidence["reconciliation"], {
            "status": "not_applicable",
            "source": "published_snapshot",
        })
        self.assertEqual(evidence["snapshot"]["snapshot_hash"], snapshot.snapshot_hash)

    @patch("kopos_connector.api.promotions.resolve_snapshot_pos_profile")
    @patch("kopos_connector.api.promotions.get_snapshot_by_version")
    def test_restricted_manual_only_sale_keeps_snapshot_identity_without_promotions(
        self, mock_get_snapshot, mock_resolve_profile
    ):
        from kopos_connector.kopos.api.fb_orders import (
            _validate_published_promotion_snapshot,
        )

        normalized, snapshot, _payload = self._stateful_promotion_fixture()
        normalized["pricing_context"].update(
            {
                "pricing_mode": "manual_only",
                "restricted_mode": True,
                "promotion_expiry_by_id": {},
            }
        )
        normalized["applied_promotions"] = []
        normalized["promotion_reconciliation_status"] = "not_applicable"
        normalized["items"][0]["discount_amount_sen"] = 0
        normalized["items"][0]["promotion_allocations"] = []
        mock_resolve_profile.return_value = "KoPOS Smoke Profile"
        mock_get_snapshot.return_value = snapshot

        evidence = _validate_published_promotion_snapshot(normalized)

        self.assertEqual(evidence["reconciliation"]["status"], "not_applicable")
        self.assertEqual(evidence["snapshot"]["snapshot_hash"], snapshot.snapshot_hash)

    @patch("kopos_connector.api.promotions.resolve_snapshot_pos_profile")
    @patch("kopos_connector.api.promotions.get_snapshot_by_version")
    def test_snapshot_priced_sale_cannot_omit_applicable_automatic_promotion(
        self, mock_get_snapshot, mock_resolve_profile
    ):
        from kopos_connector.api.promotions import (
            build_snapshot_version_from_hash,
            compute_snapshot_content_hash,
        )
        from kopos_connector.kopos.api.fb_orders import (
            _validate_published_promotion_snapshot,
        )

        normalized, snapshot, payload = self._stateful_promotion_fixture()
        payload["promotions"][0]["activation_mode"] = "automatic"
        snapshot_hash = compute_snapshot_content_hash(
            {
                "pos_profile": payload["pos_profile"],
                "promotions": payload["promotions"],
            }
        )
        snapshot_version = build_snapshot_version_from_hash(snapshot_hash)
        payload["snapshot_hash"] = snapshot_hash
        payload["snapshot_version"] = snapshot_version
        snapshot.snapshot_hash = snapshot_hash
        snapshot.snapshot_version = snapshot_version
        snapshot.snapshot_payload = json.dumps(
            payload, sort_keys=True, separators=(",", ":")
        )
        normalized["pricing_context"]["snapshot_hash"] = snapshot_hash
        normalized["pricing_context"]["snapshot_version"] = snapshot_version
        normalized["pricing_context"]["promotion_expiry_by_id"] = {}
        normalized["applied_promotions"] = []
        normalized["promotion_reconciliation_status"] = "not_applicable"
        normalized["items"][0]["discount_amount_sen"] = 0
        normalized["items"][0]["promotion_allocations"] = []
        mock_resolve_profile.return_value = "KoPOS Smoke Profile"
        mock_get_snapshot.return_value = snapshot

        with self.assertRaisesRegex(
            Exception,
            "Applied promotion ids do not exactly match server-recalculated snapshot pricing",
        ):
            _validate_published_promotion_snapshot(normalized)

    def test_offline_manual_only_sale_is_valid_without_a_promotion_snapshot(self):
        from kopos_connector.kopos.api.fb_orders import (
            _validate_offline_pricing_consistency,
        )

        _validate_offline_pricing_consistency(
            True,
            {"pricing_mode": "manual_only"},
        )

    def test_restricted_snapshot_context_requires_manual_only_mode(self):
        from kopos_connector.kopos.api.fb_orders import _normalize_pricing_context

        with self.assertRaisesRegex(
            Exception,
            "restricted promotion pricing must use manual_only pricing_mode",
        ):
            _normalize_pricing_context(
                {
                    "snapshot_version": "KOPOS-PROMO-AAAAAAAAAAAAAAAA",
                    "snapshot_hash": "a" * 64,
                    "pricing_mode": "offline_snapshot",
                    "restricted_mode": True,
                }
            )

    def test_no_promotion_sale_rejects_orphan_expiry_evidence(self):
        from kopos_connector.kopos.api.fb_orders import (
            _validate_normalized_promotion_evidence,
        )

        with self.assertRaisesRegex(
            Exception,
            "promotion expiry evidence requires applied_promotions",
        ):
            _validate_normalized_promotion_evidence(
                {
                    "offline_applied_promotion_ids": [],
                    "promotion_expiry_by_id": {"PROMO-1": None},
                },
                [],
                [{"promotion_allocations": []}],
            )

    def test_item_promotion_minimums_match_tablet_stacking_order(self):
        from kopos_connector.kopos.api.fb_orders import (
            _calculate_expected_snapshot_promotions,
        )

        normalized = {
            "sale_datetime": datetime(2026, 7, 12, 10, 1),
            "applied_promotions": [],
            "items": [
                {
                    "item_code": "ITEM-A",
                    "qty": 1,
                    "unit_price_sen": 1000,
                },
                {
                    "item_code": "ITEM-B",
                    "qty": 1,
                    "unit_price_sen": 1000,
                },
            ],
        }
        common = {
            "promotion_name": "Automatic 10%",
            "promotion_type": "item_discount",
            "activation_mode": "automatic",
            "offline_allowed": True,
            "stacking_policy": "exclusive",
            "discount_type": "percentage",
            "discount_value": 10,
            "valid_from": None,
            "valid_upto": None,
            "eligible_item_groups": [],
            "selected_pos_profiles": ["KoPOS Smoke Profile"],
            "min_amount": 0,
        }
        rules = [
            {
                **common,
                "promotion_id": "PROMO-1",
                "priority": 1,
                "eligible_items": ["ITEM-A"],
                "min_qty": 1,
            },
            {
                **common,
                "promotion_id": "PROMO-2",
                "priority": 2,
                "eligible_items": ["ITEM-A", "ITEM-B"],
                "min_qty": 2,
            },
        ]

        promotions, allocations = _calculate_expected_snapshot_promotions(
            normalized,
            rules,
            "KoPOS Smoke Profile",
        )

        self.assertEqual(
            [(row["promotion_id"], row["amount_sen"]) for row in promotions],
            [("PROMO-1", 100), ("PROMO-2", 100)],
        )
        self.assertEqual(allocations[0][0]["promotion_id"], "PROMO-1")
        self.assertEqual(allocations[1][0]["promotion_id"], "PROMO-2")

    def test_snapshot_pricing_mode_must_match_offline_sale_provenance(self):
        from kopos_connector.kopos.api.fb_orders import (
            _validate_offline_pricing_consistency,
        )

        with self.assertRaisesRegex(Exception, "must be true for offline_snapshot"):
            _validate_offline_pricing_consistency(
                False,
                {"pricing_mode": "offline_snapshot"},
            )
        with self.assertRaisesRegex(Exception, "must be false for online"):
            _validate_offline_pricing_consistency(
                True,
                {"pricing_mode": "online_snapshot"},
            )

    def test_canonical_promotion_evidence_binds_exact_snapshot_and_allocation(self):
        from kopos_connector.kopos.api.fb_orders import (
            _normalize_applied_promotions,
            _normalize_pricing_context,
            _normalize_promotion_allocations,
            _validate_normalized_promotion_evidence,
        )

        snapshot_hash = "a" * 64
        pricing_context = _normalize_pricing_context(
            {
                "snapshot_version": "KOPOS-PROMO-AAAAAAAAAAAAAAAA",
                "snapshot_hash": snapshot_hash,
                "pricing_mode": "online_snapshot",
                "restricted_mode": False,
                "offline_applied_promotion_ids": [],
                "promotion_expiry_by_id": {
                    "SMOKE-MANUAL-10-PCT": None,
                },
            }
        )
        promotions = _normalize_applied_promotions(
            [
                {
                    "promotion_id": "SMOKE-MANUAL-10-PCT",
                    "promotion_name": "SMOKE-MANUAL-10-PCT",
                    "promotion_type": "item_discount",
                    "amount_sen": 120,
                    "scope": "order",
                    "source": "snapshot",
                    "snapshot_version": "KOPOS-PROMO-AAAAAAAAAAAAAAAA",
                    "snapshot_hash": snapshot_hash,
                    "offline_applied": False,
                }
            ],
            "sen_v1",
        )
        allocations = _normalize_promotion_allocations(
            [
                {
                    "promotion_id": "SMOKE-MANUAL-10-PCT",
                    "amount_sen": 120,
                    "quantity": 1,
                    "scope": "line",
                }
            ],
            item_index=1,
            money_contract_version="sen_v1",
        )

        status = _validate_normalized_promotion_evidence(
            pricing_context,
            promotions,
            [
                {
                    "discount_amount_sen": 120,
                    "promotion_allocations": allocations,
                }
            ],
        )

        self.assertEqual(status, "matched")
        self.assertEqual(promotions[0]["amount_sen"], 120)
        self.assertEqual(allocations[0]["amount_sen"], 120)

    def test_canonical_promotion_evidence_rejects_total_mismatch(self):
        from kopos_connector.kopos.api.fb_orders import (
            _validate_normalized_promotion_evidence,
        )

        context = {
            "snapshot_version": "KOPOS-PROMO-AAAAAAAAAAAAAAAA",
            "snapshot_hash": "a" * 64,
            "pricing_mode": "online_snapshot",
            "restricted_mode": False,
            "offline_applied_promotion_ids": [],
        }
        promotions = [
            {
                "promotion_id": "SMOKE-MANUAL-10-PCT",
                "amount_sen": 120,
                "snapshot_version": context["snapshot_version"],
                "snapshot_hash": context["snapshot_hash"],
                "offline_applied": False,
            }
        ]
        items = [
            {
                "discount_amount_sen": 119,
                "promotion_allocations": [
                    {
                        "promotion_id": "SMOKE-MANUAL-10-PCT",
                        "amount_sen": 119,
                    }
                ],
            }
        ]

        with self.assertRaisesRegex(
            Exception,
            "applied promotion totals must exactly match line allocations",
        ):
            _validate_normalized_promotion_evidence(context, promotions, items)

    def test_add_modifier_preserves_explicit_non_stock_effect(self):
        from kopos_connector.kopos.doctype.fb_order.fb_order import FBOrder

        order = FBOrder()
        order.booth_warehouse = "WH-1"
        component = order.build_modifier_component(
            SimpleNamespace(
                name="FB-MOD-NOTE",
                new_item="PACKAGING-NOTE",
                target_item=None,
                qty_delta=1,
                qty_uom="Nos",
                affects_stock=0,
                instruction_text="No stock impact",
            ),
            1,
            SimpleNamespace(line_id="LINE-1", item="LATTE"),
        )

        self.assertEqual(component["affects_stock"], 0)

    @patch("kopos_connector.kopos.api.fb_orders.frappe.get_cached_doc")
    @patch("kopos_connector.kopos.api.fb_orders.frappe.db.exists")
    def test_validate_selected_modifier_preserves_offline_sale_price_snapshot(
        self, mock_exists, mock_get_cached_doc
    ):
        from kopos_connector.kopos.api.fb_orders import _validate_selected_modifier

        existing_docs = {
            ("FB Modifier Group", "FB-GRP-TEMP"),
            ("FB Modifier", "FB-MOD-ICED"),
        }
        mock_exists.side_effect = lambda doctype, name: (doctype, name) in existing_docs
        mock_get_cached_doc.side_effect = lambda doctype, name: {
            ("FB Modifier Group", "FB-GRP-TEMP"): MagicMock(name="FB-GRP-TEMP"),
            (
                "FB Modifier",
                "FB-MOD-ICED",
            ): MagicMock(
                name="FB-MOD-ICED",
                modifier_group="FB-GRP-TEMP",
                price_adjustment=1.5,
                instruction_text="Less ice",
                display_order=4,
                affects_stock=1,
                affects_recipe=0,
            ),
        }[(doctype, name)]

        result = _validate_selected_modifier(
            {
                "modifier_group": "FB-GRP-TEMP",
                "modifier": "FB-MOD-ICED",
                "price_adjustment": 99,
                "instruction_text": "",
                "sort_order": 0,
                "affects_stock": 0,
                "affects_recipe": 1,
            },
            1,
            1,
        )

        self.assertEqual(result["modifier_group"], "FB-GRP-TEMP")
        self.assertEqual(result["modifier"], "FB-MOD-ICED")
        self.assertEqual(result["price_adjustment"], 99)
        self.assertEqual(result["instruction_text"], "Less ice")
        self.assertEqual(result["sort_order"], 4)
        self.assertEqual(result["affects_stock"], 1)
        self.assertEqual(result["affects_recipe"], 0)

    @patch("kopos_connector.kopos.api.fb_orders.frappe.db.exists")
    def test_validate_selected_modifier_rejects_legacy_kopos_modifier_id(
        self, mock_exists
    ):
        from kopos_connector.kopos.api.fb_orders import _validate_selected_modifier

        mock_exists.return_value = True

        with self.assertRaises(Exception) as context:
            _validate_selected_modifier(
                {
                    "modifier_group": "FB-GRP-TEMP",
                    "modifier": "KOPOS-OPT-00001",
                    "price_adjustment": 0,
                },
                1,
                1,
            )

        self.assertIn("legacy KoPOS modifier id", str(context.exception))
        self.assertIn("FB-only modifier ids", str(context.exception))

    @patch("kopos_connector.kopos.api.fb_orders.frappe.get_doc")
    @patch("kopos_connector.kopos.api.fb_orders.frappe.get_cached_doc")
    @patch("kopos_connector.kopos.api.fb_orders.frappe.db.exists")
    def test_validate_order_item_requires_modifier_total_to_match_fb_prices(
        self, mock_exists, mock_get_cached_doc, mock_get_doc
    ):
        from kopos_connector.kopos.api.fb_orders import _validate_order_item

        existing_docs = {
            ("UOM", "Nos"),
            ("FB Modifier Group", "FB-GRP-TEMP"),
            ("FB Modifier", "FB-MOD-ICED"),
        }
        mock_exists.side_effect = lambda doctype, name: (doctype, name) in existing_docs
        mock_get_doc.return_value = MagicMock(
            name="ITEM-COFFEE", item_name="Coffee", stock_uom="Nos"
        )
        mock_get_cached_doc.side_effect = lambda doctype, name: {
            ("FB Modifier Group", "FB-GRP-TEMP"): MagicMock(name="FB-GRP-TEMP"),
            (
                "FB Modifier",
                "FB-MOD-ICED",
            ): MagicMock(
                name="FB-MOD-ICED",
                modifier_group="FB-GRP-TEMP",
                price_adjustment=1.5,
                instruction_text=None,
                display_order=2,
                affects_stock=1,
                affects_recipe=0,
            ),
        }[(doctype, name)]

        with self.assertRaises(Exception) as context:
            _validate_order_item(
                {
                    "line_id": "LINE-1",
                    "item_code": "ITEM-COFFEE",
                    "qty": 1,
                    "uom": "Nos",
                    "unit_price": 10,
                    "modifier_total": 0,
                    "discount_amount": 0,
                    "line_total": 10,
                    "selected_modifiers": [
                        {
                            "modifier_group": "FB-GRP-TEMP",
                            "modifier": "FB-MOD-ICED",
                            "price_adjustment": 1.5,
                        }
                    ],
                },
                1,
            )

        self.assertIn(
            "modifier_total_sen must equal summed modifier price adjustments",
            str(context.exception),
        )

    @patch("kopos_connector.kopos.api.fb_orders.frappe.get_cached_doc")
    @patch("kopos_connector.kopos.api.fb_orders.frappe.db.exists")
    def test_validate_selected_modifier_rejects_fb_modifier_from_another_group(
        self, mock_exists, mock_get_cached_doc
    ):
        from kopos_connector.kopos.api.fb_orders import _validate_selected_modifier

        existing_docs = {
            ("FB Modifier Group", "FB-GRP-TEMP"),
            ("FB Modifier", "FB-MOD-ICED"),
        }
        mock_exists.side_effect = lambda doctype, name: (doctype, name) in existing_docs
        mock_get_cached_doc.side_effect = lambda doctype, name: {
            ("FB Modifier Group", "FB-GRP-TEMP"): MagicMock(name="FB-GRP-TEMP"),
            ("FB Modifier", "FB-MOD-ICED"): MagicMock(
                name="FB-MOD-ICED",
                modifier_group="FB-GRP-OTHER",
                price_adjustment=0,
                instruction_text=None,
                display_order=1,
                affects_stock=0,
                affects_recipe=0,
            ),
        }[(doctype, name)]

        with self.assertRaises(Exception) as context:
            _validate_selected_modifier(
                {
                    "modifier_group": "FB-GRP-TEMP",
                    "modifier": "FB-MOD-ICED",
                    "price_adjustment": 0,
                },
                1,
                1,
            )

        self.assertIn("does not belong to FB Modifier Group", str(context.exception))


if __name__ == "__main__":
    unittest.main()
