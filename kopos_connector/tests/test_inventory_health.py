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
    def test_stock_update_status_moves_from_updating_to_attention_without_internal_terms(self) -> None:
        now = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
        recent = inventory._stock_update_status_response(
            company="JiJi",
            warehouse="Outlet - J",
            rows=[{
                "order_name": "ORDER-1",
                "sale_datetime": now - timedelta(seconds=20),
                "stock_status": "Pending",
                "projection_state": "Processing",
            }],
            latest_completed_at=None,
            availability_checked_at=None,
            now=now,
        )
        late = inventory._stock_update_status_response(
            company="JiJi",
            warehouse="Outlet - J",
            rows=[{
                "order_name": "ORDER-1",
                "sale_datetime": now - timedelta(seconds=61),
                "stock_status": "Pending",
                "projection_state": "Failed",
            }],
            latest_completed_at=None,
            availability_checked_at=None,
            now=now,
        )
        attention = inventory._stock_update_status_response(
            company="JiJi",
            warehouse="Outlet - J",
            rows=[{
                "order_name": "ORDER-1",
                "sale_datetime": now - timedelta(minutes=5),
                "stock_status": "Failed",
                "projection_state": "Dead Letter",
            }],
            latest_completed_at=None,
            availability_checked_at=None,
            now=now,
        )

        self.assertEqual(recent["state"], "updating")
        self.assertEqual(late["state"], "taking_longer")
        self.assertEqual(attention["state"], "needs_attention")
        self.assertNotIn("error", recent)
        self.assertNotIn("projection", recent)

    def test_stock_update_stays_updating_until_availability_catches_up(self) -> None:
        now = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
        completed = now - timedelta(seconds=20)
        waiting = inventory._stock_update_status_response(
            company="JiJi",
            warehouse="Outlet - J",
            rows=[],
            latest_completed_at=completed,
            availability_checked_at=completed - timedelta(seconds=5),
            now=now,
        )
        current = inventory._stock_update_status_response(
            company="JiJi",
            warehouse="Outlet - J",
            rows=[],
            latest_completed_at=completed,
            availability_checked_at=completed + timedelta(seconds=1),
            now=now,
        )

        self.assertEqual(waiting["state"], "updating")
        self.assertEqual(waiting["pending_sales"], 0)
        self.assertEqual(current["state"], "current")

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

    def test_scheduler_deadline_matches_the_five_minute_recover_cron(self) -> None:
        """The marker-stamping worker runs on "*/5", not hourly.

        Health used to describe it as hourly, so a stalled five-minute worker
        could miss seventeen runs before the deadline was even reached.
        """

        last_success = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(inventory.INVENTORY_RECOVER_INTERVAL_MINUTES, 5)
        self.assertEqual(
            inventory._next_success_deadline(last_success),
            datetime(2026, 8, 15, 12, 5, tzinfo=timezone.utc),
        )

    def test_scheduler_health_reports_the_real_cadence(self) -> None:
        scheduler = inventory._scheduler_health()
        self.assertEqual(scheduler["expected"], "every 5 minutes")
        self.assertEqual(
            scheduler["expected_interval_minutes"],
            inventory.INVENTORY_RECOVER_INTERVAL_MINUTES,
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

    def test_guided_workflow_money_uses_exact_decimal_sen(self) -> None:
        self.assertEqual(inventory._autopilot_money_sen("12.345"), 1235)
        self.assertEqual(inventory._autopilot_money_sen("0.005"), 1)
        self.assertEqual(inventory._autopilot_money_sen("not-a-number"), 0)

    def test_guided_workflow_quantities_remain_decimal_text(self) -> None:
        self.assertEqual(inventory._autopilot_decimal_text("001.2300"), "1.23")
        self.assertEqual(inventory._autopilot_decimal_text("0.000"), "0")
        self.assertEqual(inventory._autopilot_decimal_text("NaN"), "0")

    def test_guided_finance_separates_waste_and_transfer(self) -> None:
        self.assertEqual(inventory._autopilot_movement_kind({
            "actual_qty": "-2.5",
            "custom_kopos_inventory_command_id": "fb-waste:demo-1",
            "purpose": "Material Issue",
        }), "waste")
        self.assertEqual(inventory._autopilot_movement_kind({
            "actual_qty": "-2.5",
            "custom_kopos_inventory_command_id": "",
            "purpose": "Material Transfer",
        }), "transfer")
        self.assertEqual(inventory._autopilot_movement_kind({
            "actual_qty": "2.5",
            "custom_kopos_inventory_command_id": "",
            "purpose": "Material Receipt",
        }), "received")
        self.assertEqual(inventory._autopilot_movement_quantity({
            "actual_qty": "0",
            "qty_after_transaction": "1400.000",
            "voucher_type": "Stock Reconciliation",
        }), "1400")
        self.assertEqual(inventory._autopilot_movement_kind({
            "actual_qty": "0",
            "stock_value_difference": "1210.20",
            "voucher_type": "Stock Reconciliation",
            "custom_kopos_inventory_command_id": "",
            "purpose": "",
        }), "received")

    def test_projection_next_steps_never_tell_users_to_recreate_work(self) -> None:
        self.assertIn("Do not delete this saved update", inventory._autopilot_projection_next_action("Dead Letter"))
        self.assertIn("JiJi will try again", inventory._autopilot_projection_next_action("Failed"))
        self.assertIn("Refresh in a few minutes", inventory._autopilot_projection_next_action("Processing"))

    def test_guided_update_copy_hides_internal_failure_details(self) -> None:
        visible_copy = " ".join(filter(None, (
            inventory._autopilot_projection_plain_reason("Failed"),
            inventory._autopilot_projection_plain_reason("Dead Letter"),
            inventory._autopilot_projection_next_action("Failed"),
            inventory._autopilot_projection_next_action("Dead Letter"),
        )))
        for internal_term in ("worker", "scheduler", "projection", "payload", "dead letter", "retry"):
            self.assertNotIn(internal_term, visible_copy.lower())
        self.assertEqual(inventory._autopilot_projection_state_label("Succeeded"), "Finished")
        self.assertEqual(inventory._autopilot_projection_state_label("Failed"), "Needs help")
        self.assertEqual(inventory._autopilot_projection_state_label("Dead Letter"), "Stopped for safety")

if __name__ == "__main__":
    import unittest

    unittest.main()
