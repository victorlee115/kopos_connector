from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from kopos_connector.tests.fake_frappe import install_fake_frappe_modules


install_fake_frappe_modules()

import frappe

from kopos_connector.api import inventory
from kopos_connector.kopos.doctype.fb_inventory_policy.fb_inventory_policy import _finite_decimal
from kopos_connector.kopos.services.inventory_autopilot.cutover import (
    device_activation_failures,
    monitoring_owner_failures,
    opening_reconciliation_failure,
)


class InventoryCutoverTests(TestCase):
    def test_policy_ceilings_use_finite_decimal_not_float(self) -> None:
        self.assertEqual(_finite_decimal("0.1000000000000000001", "ceiling"), Decimal("0.1000000000000000001"))
        with self.assertRaises(frappe.ValidationError):
            _finite_decimal("NaN", "ceiling")
        with self.assertRaises(frappe.ValidationError):
            _finite_decimal("Infinity", "ceiling")
        with self.assertRaises(frappe.ValidationError):
            _finite_decimal("not-a-number", "ceiling")

    def test_opening_reconciliation_requires_submitted_matching_lines(self) -> None:
        reconciliation = SimpleNamespace(
            docstatus=1,
            company="JiJi",
            items=[SimpleNamespace(item_code="MILK", warehouse="Outlet - KL")],
        )
        self.assertIsNone(
            opening_reconciliation_failure(
                reconciliation,
                company="JiJi",
                warehouse="Outlet - KL",
            )
        )
        reconciliation.items[0].warehouse = "Other Outlet"
        self.assertEqual(
            opening_reconciliation_failure(
                reconciliation,
                company="JiJi",
                warehouse="Outlet - KL",
            ),
            "opening_stock_reconciliation_warehouse_mismatch",
        )
        reconciliation.items[0].warehouse = "Outlet - KL"
        reconciliation.docstatus = 0
        self.assertEqual(
            opening_reconciliation_failure(
                reconciliation,
                company="JiJi",
                warehouse="Outlet - KL",
            ),
            "opening_stock_reconciliation_not_submitted",
        )

    def test_device_preflight_requires_exact_ack_and_clean_command_queues(self) -> None:
        now = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)
        row = {
            "name": "TABLET-1",
            "config_version": "cfg-2",
            "inventory_config_version": "cfg-1",
            "inventory_report_received_at": now.isoformat(),
            "inventory_observed_at": now.isoformat(),
            "inventory_catalog_version": "catalog-2",
            "inventory_overlay_version": "overlay-2",
            "inventory_overlay_hash": "hash-2",
            "inventory_sales_pending": 1,
            "inventory_sales_syncing": 0,
            "inventory_sales_failed": 0,
            "inventory_sales_dead_letter": 0,
            "inventory_commands_pending": 1,
            "inventory_commands_syncing": 0,
            "inventory_commands_failed": 0,
            "inventory_commands_dead_letter": 0,
        }
        failures = device_activation_failures(
            [row],
            max_source_age_minutes=30,
            now=now,
            overlay_is_current=lambda _row: True,
        )
        self.assertIn("device_config_not_current:TABLET-1", failures)
        self.assertIn("device_sales_queue_not_clean:TABLET-1", failures)
        self.assertIn("device_inventory_queue_not_clean:TABLET-1", failures)

        row["config_version"] = "cfg-1"
        row["inventory_sales_pending"] = 0
        row["inventory_commands_pending"] = 0
        self.assertEqual(
            device_activation_failures(
                [row],
                max_source_age_minutes=30,
                now=now,
                overlay_is_current=lambda _row: True,
            ),
            (),
        )
        row["inventory_report_received_at"] = (now - timedelta(minutes=31)).isoformat()
        self.assertIn(
            "device_report_stale:TABLET-1",
            device_activation_failures(
                [row],
                max_source_age_minutes=30,
                now=now,
                overlay_is_current=lambda _row: True,
            ),
        )

    def test_device_preflight_normalizes_mixed_business_and_offset_timestamps(self) -> None:
        # Frappe stores report receipt in the protected site timezone without an
        # offset, while the tablet observation carries an explicit UTC offset.
        # These represent 11:30 and 11:45 Asia/Kuala_Lumpur respectively.
        now = datetime(2026, 8, 15, 4, 0, tzinfo=timezone.utc)
        row = {
            "name": "TABLET-1",
            "config_version": "cfg-1",
            "inventory_config_version": "cfg-1",
            "inventory_report_received_at": "2026-08-15 11:30:00",
            "inventory_observed_at": "2026-08-15T03:45:00+00:00",
            "inventory_catalog_version": "catalog-2",
            "inventory_overlay_version": "overlay-2",
            "inventory_overlay_hash": "hash-2",
        }

        self.assertIn(
            "device_report_stale:TABLET-1",
            device_activation_failures(
                [row],
                max_source_age_minutes=20,
                now=now,
                overlay_is_current=lambda _row: True,
            ),
        )

    def test_monitoring_and_owner_preflight_is_explicit(self) -> None:
        failures = monitoring_owner_failures(
            {},
            automation_identity_ready=False,
            purchase_review_owner=None,
        )
        self.assertEqual(
            failures,
            (
                "monitor_destination_not_configured",
                "inventory_automation_user_not_configured",
                "inventory_purchase_review_owner_not_configured",
            ),
        )
        self.assertEqual(
            monitoring_owner_failures(
                {"kopos_inventory_monitor_destination": "watchdog"},
                automation_identity_ready=True,
                purchase_review_owner="director@example.com",
            ),
            (),
        )

    def test_activation_is_server_owned_review_first_and_idempotent(self) -> None:
        policy = SimpleNamespace(
            name="FB-POLICY-JIJI-OUTLET",
            company="JiJi",
            warehouse="Outlet - KL",
            automation_state="Review First",
            max_source_age_minutes=30,
            cutover_token="",
            cutover_at="",
            opening_stock_reconciliation="",
            save=MockSave(),
        )
        reconciliation = SimpleNamespace(
            docstatus=1,
            company="JiJi",
            items=[SimpleNamespace(item_code="MILK", warehouse="Outlet - KL")],
        )
        with patch.object(frappe.db, "sql", side_effect=[[], []]), patch.object(
            frappe, "get_doc", side_effect=[policy, reconciliation]
        ), patch.object(
            inventory, "get_menu_authoring_summary", return_value={"ready": True}
        ), patch.object(
            inventory, "device_activation_failures", return_value=()
        ), patch.object(
            inventory, "outlet_erp_role_failures", return_value=()
        ), patch.object(
            inventory, "monitoring_owner_failures", return_value=()
        ), patch.object(
            inventory, "automation_identity_is_configured", return_value=True
        ), patch.object(
            inventory, "purchase_review_owner", return_value="director@example.com"
        ):
            result = inventory.activate_inventory_cutover(
                policy=policy.name,
                opening_stock_reconciliation="MAT-RECON-1",
            )

        self.assertEqual(result["status"], "activated")
        self.assertEqual(result["automation_state"], "Review First")
        self.assertEqual(policy.cutover_token, "token-123")
        self.assertTrue(policy.cutover_at)
        self.assertEqual(policy.opening_stock_reconciliation, "MAT-RECON-1")
        policy.save.assert_called_once_with(ignore_permissions=True)

        existing = SimpleNamespace(
            name=policy.name,
            company=policy.company,
            warehouse=policy.warehouse,
            automation_state="Paused",
            cutover_token="immutable-token",
            cutover_at=datetime(2026, 8, 15, 12, 0),
            opening_stock_reconciliation="MAT-RECON-1",
        )
        with patch.object(frappe.db, "sql", return_value=[]), patch.object(
            frappe, "get_doc", return_value=existing
        ):
            replay = inventory.activate_inventory_cutover(policy=existing.name)
        self.assertEqual(replay["status"], "already_active")
        self.assertEqual(replay["cutover_token"], "immutable-token")
        self.assertEqual(replay["automation_state"], "Paused")

        with patch.object(frappe.db, "sql", return_value=[]), patch.object(
            frappe, "get_doc", return_value=existing
        ), self.assertRaises(frappe.ValidationError):
            inventory.activate_inventory_cutover(
                policy=existing.name,
                opening_stock_reconciliation="MAT-RECON-2",
            )


class MockSave:
    def __init__(self) -> None:
        self.assert_called = False
        self.calls: list[dict[str, object]] = []

    def __call__(self, **kwargs: object) -> None:
        self.assert_called = True
        self.calls.append(kwargs)

    def assert_called_once_with(self, **kwargs: object) -> None:
        if self.calls != [kwargs]:
            raise AssertionError(f"expected one save call {kwargs!r}, got {self.calls!r}")


if __name__ == "__main__":
    import unittest

    unittest.main()
