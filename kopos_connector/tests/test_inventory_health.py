from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest import TestCase

from kopos_connector.tests.fake_frappe import install_fake_frappe_modules


install_fake_frappe_modules()

from kopos_connector.api import inventory
from kopos_connector.kopos.services.inventory_autopilot.health_monitor import (
    health_blocks_rollout,
)


class InventoryHealthReadModelTests(TestCase):
    def test_oldest_projection_age_ignores_succeeded_history(self) -> None:
        now = datetime(2026, 8, 15, 12, 0)
        rows = [
            {"state": "Succeeded", "oldest_created_at": now - timedelta(days=90)},
            {"state": "Reversed", "oldest_created_at": now - timedelta(days=45)},
            {"state": "Pending", "oldest_created_at": now - timedelta(minutes=17)},
        ]
        self.assertEqual(inventory._active_projection_oldest_age(rows, now=now), 17)

    def test_only_completed_projection_history_has_no_backlog_age(self) -> None:
        now = datetime(2026, 8, 15, 12, 0)
        self.assertIsNone(
            inventory._active_projection_oldest_age(
                [{"state": "Succeeded", "oldest_created_at": now - timedelta(days=90)}],
                now=now,
            )
        )

    def test_device_dirty_includes_all_inventory_command_states(self) -> None:
        for fieldname in (
            "inventory_commands_pending",
            "inventory_commands_syncing",
            "inventory_commands_failed",
            "inventory_commands_dead_letter",
        ):
            row = {field: 0 for field in (
                "inventory_sales_pending",
                "inventory_sales_syncing",
                "inventory_sales_failed",
                "inventory_sales_dead_letter",
                "inventory_commands_pending",
                "inventory_commands_syncing",
                "inventory_commands_failed",
                "inventory_commands_dead_letter",
            )}
            row[fieldname] = 1
            self.assertTrue(inventory._device_is_dirty(row), fieldname)

    def test_scheduler_deadline_is_one_hour_after_last_success(self) -> None:
        last_success = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(
            inventory._next_success_deadline(last_success),
            datetime(2026, 8, 15, 13, 0, tzinfo=timezone.utc),
        )

    def test_health_age_converts_explicit_utc_to_business_timezone(self) -> None:
        self.assertEqual(
            inventory._age_minutes(
                datetime(2026, 8, 15, 4, 0, tzinfo=timezone.utc),
                datetime(2026, 8, 15, 12, 0),
            ),
            0,
        )

    def test_cutover_rollout_gate_consumes_critical_health(self) -> None:
        self.assertTrue(health_blocks_rollout({
            "exceptions": {"critical_reasons": ["inventory_projection_dead_letter"]},
            "draft_purchase_order_safety": "safe",
        }))
        self.assertTrue(health_blocks_rollout({
            "exceptions": {"critical_reasons": []},
            "draft_purchase_order_safety": "unsafe",
        }))
        self.assertFalse(health_blocks_rollout({
            "exceptions": {"critical_reasons": [], "warning_reasons": ["inventory_device_stale"]},
            "draft_purchase_order_safety": "safe",
        }))

if __name__ == "__main__":
    import unittest

    unittest.main()
