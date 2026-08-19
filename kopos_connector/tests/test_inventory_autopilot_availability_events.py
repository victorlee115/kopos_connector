from __future__ import annotations

from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from kopos_connector.tests.fake_frappe import install_fake_frappe_modules


install_fake_frappe_modules()

from kopos_connector.kopos.services.inventory_autopilot import availability_events


class InventoryAvailabilityEventTests(TestCase):
    def test_stock_entry_warehouses_are_deduplicated_but_sale_issue_is_excluded(self) -> None:
        document = SimpleNamespace(
            doctype="Stock Entry",
            custom_fb_order=None,
            items=[
                SimpleNamespace(s_warehouse="Outlet A", t_warehouse="Transit"),
                SimpleNamespace(s_warehouse="Outlet A", t_warehouse="Outlet B"),
            ],
        )
        self.assertEqual(
            availability_events._affected_warehouses(document),
            ["Outlet A", "Outlet B", "Transit"],
        )

        document.custom_fb_order = "FB-ORDER-1"
        self.assertEqual(availability_events._affected_warehouses(document), [])

    def test_warehouse_schedule_is_debounced_for_sixty_seconds(self) -> None:
        redis_client = Mock()
        redis_client.set.side_effect = [True, False]
        with (
            patch.object(availability_events, "_redis_client", return_value=redis_client),
            patch.object(availability_events.frappe, "enqueue", create=True) as enqueue,
        ):
            self.assertTrue(
                availability_events.schedule_warehouse_availability_recheck("Outlet A")
            )
            self.assertFalse(
                availability_events.schedule_warehouse_availability_recheck("Outlet A")
            )

        self.assertEqual(enqueue.call_count, 1)
        self.assertEqual(redis_client.set.call_args.kwargs["ex"], 60)
        self.assertTrue(redis_client.set.call_args.kwargs["nx"])
        self.assertTrue(enqueue.call_args.kwargs["enqueue_after_commit"])

    def test_recheck_calls_only_the_requested_warehouse(self) -> None:
        with (
            patch.object(
                availability_events,
                "create_reliable_automation_holds",
                return_value=1,
            ) as create,
            patch.object(
                availability_events,
                "restore_automation_holds",
                return_value=2,
            ) as restore,
            patch.object(
                availability_events,
                "record_stale_automation_hold_exceptions",
                return_value=3,
            ) as stale,
            patch.object(availability_events.frappe.db, "commit"),
        ):
            result = availability_events.reevaluate_warehouse_availability("Outlet A")

        create.assert_called_once_with(warehouse="Outlet A")
        restore.assert_called_once_with(warehouse="Outlet A")
        stale.assert_called_once_with(warehouse="Outlet A")
        self.assertEqual(result["created_holds"], 1)
        self.assertEqual(result["released_holds"], 2)
        self.assertEqual(result["stale_exceptions"], 3)

    def test_hourly_recovery_rechecks_each_configured_warehouse(self) -> None:
        with (
            patch.object(availability_events.frappe.db, "exists", return_value=True),
            patch.object(
                availability_events.frappe,
                "get_all",
                return_value=[{"warehouse": "Outlet B"}, {"warehouse": "Outlet A"}, {"warehouse": "Outlet A"}],
            ),
            patch.object(
                availability_events,
                "schedule_warehouse_availability_recheck",
                side_effect=[True, False],
            ) as schedule,
        ):
            result = availability_events.recover_availability_hourly()

        self.assertEqual(result, {"warehouses": 2, "scheduled": 1, "failed": 1})
        self.assertEqual(
            [call.args for call in schedule.call_args_list],
            [("Outlet A",), ("Outlet B",)],
        )
