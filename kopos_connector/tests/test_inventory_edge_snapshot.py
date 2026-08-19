from __future__ import annotations

import inspect
import unittest
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from kopos_connector.tests.fake_frappe import install_fake_frappe_modules

install_fake_frappe_modules()

import frappe

from kopos_connector.api import inventory
from kopos_connector.kopos.services.inventory_autopilot.edge_snapshot import (
    _attach_runout,
    _safe_holds,
    _safe_task,
    attach_bounded_tasks,
    build_edge_inventory_snapshot,
    normalize_edge_query,
)
from kopos_connector.kopos.services.inventory_autopilot.availability_capacity import CapacityResult


class InventoryEdgeSnapshotTest(unittest.TestCase):
    def test_query_is_bounded(self) -> None:
        self.assertEqual(normalize_edge_query("  milk  ", "100"), ("milk", 100))
        self.assertEqual(normalize_edge_query(), ("", 50))
        with self.assertRaisesRegex(frappe.ValidationError, "characters or fewer"):
            normalize_edge_query("x" * 81)
        with self.assertRaisesRegex(frappe.ValidationError, "between 1 and 100"):
            normalize_edge_query(limit="101")

    def test_task_allow_list_removes_financial_and_supplier_fields(self) -> None:
        task = _safe_task(
            {
                "kind": "receiving",
                "document": "PO-1",
                "title": "Receive supplier delivery",
                "supplier": "Private Supplier Name",
                "rate": Decimal("12.34"),
                "value": Decimal("999"),
                "lines": [{
                    "item_code": "MILK",
                    "item_id": "MILK",
                    "qty": Decimal("2.5"),
                    "rate": Decimal("12.34"),
                    "amount": Decimal("30.85"),
                }],
            },
            max_lines=100,
        )
        self.assertEqual(task["document"], "PO-1")
        self.assertEqual(task["lines"][0]["qty"], "2.5")
        self.assertEqual(task["lines"][0]["item_id"], "MILK")
        self.assertNotIn("supplier", task)
        self.assertNotIn("rate", task)
        self.assertNotIn("value", task)
        self.assertNotIn("rate", task["lines"][0])
        self.assertNotIn("amount", task["lines"][0])

    def test_edge_tasks_preserve_preparation_alert_authority_and_dedicated_count(self) -> None:
        snapshot = {"tasks": []}
        result = attach_bounded_tasks(
            snapshot,
            {
                "tasks": [{
                    "kind": "preparation",
                    "document": "BOM-COLD-FOAM",
                    "bom_no": "BOM-COLD-FOAM",
                    "preparation_alert": True,
                    "preparation_fingerprint": "alert-fingerprint",
                    "batch_qty": Decimal("12"),
                    "min_ready_qty": Decimal("4"),
                    "trigger_qty": Decimal("3"),
                    "current_qty": Decimal("2"),
                    "lead_minutes": 20,
                    "rate": Decimal("99.00"),
                }],
                "count_task": {
                    "name": "COUNT-0001",
                    "revision": 3,
                    "warehouse": "Outlet - J",
                    "assignee": "staff@example.com",
                    "stock_watermark": "2026-08-15T02:00:00+08:00",
                    "lines": [{
                        "item_id": "MILK",
                        "item_name": "Milk",
                        "uom": "L",
                        "stock_uom": "L",
                        "purchase_uom": "Carton",
                        "conversion_factor": Decimal("12"),
                        "expected_quantity": Decimal("10"),
                    }],
                },
            },
        )

        preparation = result["tasks"][0]
        self.assertEqual(result["tasks_status"], "ok")
        self.assertEqual(preparation["preparation_alert"], True)
        self.assertEqual(preparation["preparation_fingerprint"], "alert-fingerprint")
        self.assertEqual(preparation["batch_qty"], "12")
        self.assertEqual(preparation["current_qty"], "2")
        self.assertNotIn("rate", preparation)
        self.assertEqual(result["count_task"]["name"], "COUNT-0001")
        self.assertEqual(result["count_task"]["lines"][0]["conversion_factor"], "12")
        self.assertNotIn("expected_quantity", result["count_task"]["lines"][0])

    def test_edge_tasks_derive_count_task_for_older_task_reader(self) -> None:
        result = attach_bounded_tasks(
            {"tasks": []},
            {
                "tasks": [{
                    "kind": "count",
                    "document": "COUNT-0002",
                    "revision": 4,
                    "warehouse": "Outlet - J",
                    "assignee": "staff@example.com",
                    "stock_watermark": "watermark",
                    "lines": [{"item_id": "MILK", "uom": "L", "stock_uom": "L", "purchase_uom": "Carton", "conversion_factor": "12"}],
                }],
            },
        )

        self.assertEqual(result["count_task"]["name"], "COUNT-0002")
        self.assertEqual(result["count_task"]["lines"][0]["purchase_uom"], "Carton")

    def test_edge_tasks_unavailable_keeps_commercial_snapshot_and_clears_partial_tasks(self) -> None:
        result = attach_bounded_tasks(
            {
                "status": "ok",
                "tasks": [{"kind": "count", "document": "STALE"}],
                "count_task": {"name": "STALE"},
            },
            {
                "tasks_status": "unavailable",
                "tasks_error_label": "Assigned inventory tasks are temporarily unavailable. Refresh the tablet or ask a manager.",
                "tasks": [{"kind": "count", "document": "SHOULD-NOT-LEAK"}],
                "count_task": {"name": "SHOULD-NOT-LEAK"},
            },
        )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["tasks_status"], "unavailable")
        self.assertEqual(result["tasks"], [])
        self.assertIsNone(result["count_task"])
        self.assertIn("temporarily unavailable", result["tasks_error_label"])

    def test_health_count_review_projection_exposes_operational_breakdown_only(self) -> None:
        fields = [
            "observation_id", "warehouse", "status", "lines_json", "reconciliation",
            "variance_requires_director",
        ]
        row = {
            "name": "OBS-1",
            "observation_id": "OBS-1",
            "task_id": "TASK-1",
            "task_revision": 2,
            "warehouse": "Outlet - J",
            "observed_at": "2026-08-16T10:00:00+08:00",
            "lines_json": '[{"item_id":"MILK","item_name":"Milk","stock_uom":"L","purchase_uom":"Carton","conversion_factor":"12","full_packs":"1","loose_quantity":"2","total_quantity":"14"}]',
            "status": "Review",
            "reconciliation": "",
            "variance_percent": "16.67",
            "variance_requires_director": 1,
        }
        meta = SimpleNamespace(fields=[SimpleNamespace(fieldname=name) for name in fields])
        with patch.object(inventory.frappe.db, "exists", return_value=True), patch.object(
            inventory.frappe, "get_meta", return_value=meta
        ), patch.object(inventory.frappe, "get_all", return_value=[row]), patch.object(
            inventory, "_count_line_variance_percent", return_value="16.67"
        ):
            result = inventory._health_count_director_reviews(
                "Outlet - J",
                now=datetime(2026, 8, 16, 3, 0),
            )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["open"], 1)
        self.assertEqual(result["items"][0]["lines"][0]["full_packs"], "1")
        self.assertEqual(result["items"][0]["lines"][0]["loose_quantity"], "2")
        self.assertNotIn("variance_value", result["items"][0])
        self.assertNotIn("count_variance_value_ceiling", result["items"][0])

    def test_runout_is_null_until_target_is_reliable(self) -> None:
        target = {
            "stock_item": "MILK",
            "reliability": "Please check",
            "reliability_reason": "Stock evidence is stale",
            "usable_qty": "10",
            "runout": None,
            "runout_reason": "",
        }
        _attach_runout(
            target,
            forecast_map={"MILK": Decimal("2")},
            forecast_reason="",
        )
        self.assertIsNone(target["runout"])
        self.assertEqual(target["runout_reason"], "Stock evidence is stale")

    def test_edge_endpoint_requires_bound_device_and_supports_search_limit(self) -> None:
        signature = inspect.signature(inventory.get_edge_snapshot)
        self.assertIn("search", signature.parameters)
        self.assertIn("limit", signature.parameters)
        self.assertIn("device_id", signature.parameters)

        with (
            patch.object(inventory, "require_device_operational_scope", return_value=(object(), object())),
            patch.object(
                inventory,
                "build_catalog_payload",
                return_value={"sync_mode": "unchanged"},
            ),
            patch.object(
                inventory,
                "build_edge_inventory_snapshot",
                return_value={"schema_version": "inventory-edge-v1", "tasks": []},
            ) as build_snapshot,
            patch.object(inventory, "_get_inventory_tasks", return_value={"tasks": []}),
            patch.object(inventory, "_set_health_marker"),
        ):
            inventory.require_device_operational_scope.return_value = (
                object(),
                type("Profile", (), {"company": "JiJi Sdn Bhd", "warehouse": "Outlet - J"})(),
            )
            inventory.get_edge_snapshot(device_id="DEVICE-1", search="milk", limit="1")
        build_snapshot.assert_called_once_with(
            company="JiJi Sdn Bhd",
            warehouse="Outlet - J",
            search="milk",
            limit="1",
        )

        with (
            patch.object(
                inventory,
                "require_device_operational_scope",
                return_value=(object(), type("Profile", (), {"company": "C", "warehouse": "W"})()),
            ),
            patch.object(inventory, "build_catalog_payload", return_value={"sync_mode": "full"}),
            patch.object(inventory, "build_edge_inventory_snapshot", side_effect=RuntimeError("database detail")),
            patch.object(inventory, "_get_inventory_tasks", side_effect=RuntimeError("task detail")),
            patch.object(inventory, "log_sanitized_error"),
            patch.object(inventory, "_set_health_marker"),
        ):
            fallback = inventory.get_edge_snapshot(device_id="DEVICE-1")
        self.assertEqual(fallback["sync_mode"], "full")
        self.assertEqual(fallback["inventory_snapshot"]["status"], "unavailable")
        self.assertEqual(fallback["inventory_snapshot"]["tasks"], [])
        self.assertEqual(fallback["inventory_snapshot"]["tasks_status"], "unavailable")
        self.assertIsNone(fallback["inventory_snapshot"]["count_task"])

    def test_inventory_task_reader_returns_count_task_alongside_task_list(self) -> None:
        count_task = {
            "name": "COUNT-0001",
            "revision": 2,
            "warehouse": "Outlet - J",
            "assignee": "staff@example.com",
            "stock_watermark": "watermark",
            "lines": [{"item_id": "MILK", "uom": "L"}],
        }
        with patch.object(inventory, "require_device_context", return_value=object()), patch.object(
            inventory,
            "resolve_catalog_pos_profile",
            return_value={"company": "JiJi", "warehouse": "Outlet - J"},
        ), patch.object(inventory, "_preparation_tasks_visible_to_device", return_value=False), patch.object(
            inventory,
            "_get_count_task",
            return_value={"status": "ok", "task": count_task},
        ):
            result = inventory._get_inventory_tasks(device_id="DEVICE-1")

        self.assertEqual(result["count_task"], count_task)
        self.assertEqual(result["tasks"][0]["kind"], "count")

    def test_inventory_task_reader_keeps_blocked_preparation_setup_visible(self) -> None:
        blocked_alert = {
            "kind": "preparation",
            "status": "alert",
            "title": "Batch preparation setup needed",
            "document": "Setup required",
            "item_name": "Batch preparation setup needed",
            "blocked_reason": "Batch preparation setup is incomplete.",
            "preparation_instructions": "Ask a Company Director to complete setup.",
            "fingerprint": "setup-fingerprint",
            "revision": "setup:setup-fingerprint",
        }
        work_order_meta = SimpleNamespace(has_field=lambda field: field == "custom_kopos_preparation_fingerprint")
        with patch.object(inventory, "require_device_context", return_value=SimpleNamespace(name="DEVICE-1")), patch.object(
            inventory, "resolve_catalog_pos_profile", return_value={"company": "JiJi", "warehouse": "Outlet - J"}
        ), patch.object(inventory, "_preparation_tasks_visible_to_device", return_value=True), patch.object(
            inventory, "_get_count_task", return_value={"task": None}
        ), patch.object(inventory, "derived_preparation_alerts", return_value=[blocked_alert]), patch.object(
            inventory.frappe.db,
            "exists",
            side_effect=lambda doctype, name=None, *args, **kwargs: (
                doctype == "Work Order" or (doctype == "DocType" and name == "Work Order")
            ),
        ), patch.object(inventory.frappe, "get_meta", return_value=work_order_meta), patch.object(
            inventory.frappe, "get_all", return_value=[]
        ):
            result = inventory._get_inventory_tasks(device_id="DEVICE-1")

        self.assertEqual(len(result["tasks"]), 1)
        self.assertEqual(result["tasks"][0]["title"], "Batch preparation setup needed")
        self.assertEqual(result["tasks"][0]["document"], "Setup required")
        self.assertIn("setup is incomplete", result["tasks"][0]["blocked_reason"])
        self.assertTrue(result["tasks"][0]["revision"])

    def test_snapshot_is_bounded_and_contains_operational_values_only(self) -> None:
        with (
            patch(
                "kopos_connector.kopos.services.inventory_autopilot.edge_snapshot._policy",
                return_value={
                    "automation_state": "Review First",
                    "max_source_age_minutes": 30,
                    "cutover_token": "cutover-1",
                    "cutover_at": "2026-01-01 00:00:00",
                },
            ),
            patch(
                "kopos_connector.kopos.services.inventory_autopilot.edge_snapshot._load_modifier_rows",
                return_value=[
                    {
                        "name": "MOD-1", "modifier_group": "GROUP-1", "modifier_name": "Large",
                        "target_item": "MILK", "active": 1,
                    },
                    {
                        "name": "MOD-2", "modifier_group": "GROUP-1", "modifier_name": "Extra",
                        "target_item": "MILK", "active": 1,
                    },
                ],
            ),
            patch(
                "kopos_connector.kopos.services.inventory_autopilot.edge_snapshot._load_item_rows",
                return_value=([
                    {"name": "MILK", "item_name": "Milk", "stock_uom": "L", "disabled": 0},
                    {"name": "SYRUP", "item_name": "Syrup", "stock_uom": "L", "disabled": 0},
                ], True),
            ),
            patch(
                "kopos_connector.kopos.services.inventory_autopilot.edge_snapshot._load_bin_rows",
                return_value={"MILK": {"actual_qty": "5", "reserved_qty": "1", "modified": "2024-01-01T00:00:00"}},
            ),
            patch(
                "kopos_connector.kopos.services.inventory_autopilot.edge_snapshot._load_rule_map",
                return_value={},
            ),
            patch(
                "kopos_connector.kopos.services.inventory_autopilot.edge_snapshot._safe_holds",
                return_value=[],
            ),
            patch(
                "kopos_connector.kopos.services.inventory_autopilot.edge_snapshot._reliable_forecasts",
                return_value=({}, "Forecast is not ready"),
            ),
            patch(
                "kopos_connector.kopos.services.inventory_autopilot.edge_snapshot._critical_exception_exists",
                return_value=False,
            ),
            patch(
                "kopos_connector.kopos.services.inventory_autopilot.edge_snapshot._projection_backlog_exists",
                return_value=False,
            ),
        ):
            snapshot = build_edge_inventory_snapshot(
                company="JiJi Sdn Bhd",
                warehouse="Outlet - J",
                limit=1,
                now=datetime(2026, 3, 13, 18, 5),
            )
        self.assertLessEqual(len(snapshot["items"]), 1)
        self.assertLessEqual(len(snapshot["modifier_options"]), 1)
        self.assertTrue(snapshot["truncated"]["items"])
        self.assertIsNone(snapshot["items"][0]["runout"])
        self.assertEqual(snapshot["items"][0]["actual_qty"], "5")
        self.assertEqual(snapshot["items"][0]["freshness"], "current")
        self.assertEqual(snapshot["items"][0]["last_stock_movement_at"], "2024-01-01T00:00:00+08:00")
        forbidden = {"rate", "price", "amount", "valuation_rate", "cogs", "margin", "value_ceiling"}
        self.assertTrue(forbidden.isdisjoint(snapshot["items"][0]))

    def test_catalog_made_to_order_target_uses_shared_recipe_capacity(self) -> None:
        with (
            patch(
                "kopos_connector.kopos.services.inventory_autopilot.edge_snapshot._policy",
                return_value={"automation_state": "Review First", "cutover_token": "cutover-1", "cutover_at": "2026-01-01 00:00:00"},
            ),
            patch(
                "kopos_connector.kopos.services.inventory_autopilot.edge_snapshot._load_modifier_rows",
                return_value=[],
            ),
            patch(
                "kopos_connector.kopos.services.inventory_autopilot.edge_snapshot._load_item_rows",
                return_value=(
                    [
                        {"name": "MONT-BLANC", "item_name": "Mont Blanc", "stock_uom": "Nos", "is_stock_item": 0, "disabled": 0},
                        {"name": "COLD-FOAM", "item_name": "Cold Foam", "stock_uom": "Nos", "is_stock_item": 1, "disabled": 0},
                    ],
                    False,
                ),
            ),
            patch(
                "kopos_connector.kopos.services.inventory_autopilot.edge_snapshot._load_bin_rows",
                return_value={"COLD-FOAM": {"actual_qty": "8", "reserved_qty": "0"}},
            ),
            patch(
                "kopos_connector.kopos.services.inventory_autopilot.edge_snapshot._load_rule_map",
                return_value={("Item", "MONT-BLANC"): "Warn"},
            ),
            patch(
                "kopos_connector.kopos.services.inventory_autopilot.edge_snapshot._safe_holds",
                return_value=[],
            ),
            patch(
                "kopos_connector.kopos.services.inventory_autopilot.edge_snapshot.target_capacity",
                return_value=CapacityResult(
                    target_type="Item",
                    target_id="MONT-BLANC",
                    capacity=Decimal("8"),
                    reliable=True,
                    reason="Current recipe stock evidence covers the frozen recipe components",
                    requirements={"COLD-FOAM": Decimal("1")},
                ),
            ) as shared_capacity,
            patch(
                "kopos_connector.kopos.services.inventory_autopilot.edge_snapshot._reliable_forecasts",
                return_value=({}, "Forecast is not ready"),
            ),
            patch(
                "kopos_connector.kopos.services.inventory_autopilot.edge_snapshot._critical_exception_exists",
                return_value=False,
            ),
            patch(
                "kopos_connector.kopos.services.inventory_autopilot.edge_snapshot._projection_backlog_exists",
                return_value=False,
            ),
        ):
            snapshot = build_edge_inventory_snapshot(
                company="JiJi Sdn Bhd",
                warehouse="Outlet - J",
                limit=2,
                now=datetime(2026, 3, 13, 18, 5),
                catalog_item_ids={"MONT-BLANC"},
            )

        mont_blanc = next(item for item in snapshot["items"] if item["target_id"] == "MONT-BLANC")
        cold_foam = next(item for item in snapshot["items"] if item["target_id"] == "COLD-FOAM")
        self.assertTrue(mont_blanc["is_catalog_target"])
        self.assertEqual(mont_blanc["sellable_capacity"], "8")
        self.assertIsNone(mont_blanc["stock_item"])
        self.assertEqual(cold_foam["stock_item"], "COLD-FOAM")
        self.assertEqual(cold_foam["usable_qty"], "8")
        shared_capacity.assert_called_once_with(
            target_type="Item",
            target_id="MONT-BLANC",
            company="JiJi Sdn Bhd",
            warehouse="Outlet - J",
            at_time=datetime(2026, 3, 13, 18, 5),
        )

    def test_edge_hold_marks_only_matching_pos_manual_hold_as_manager_owned(self) -> None:
        with (
            patch.object(frappe.db, "exists", return_value=True),
            patch(
                "kopos_connector.kopos.services.inventory_autopilot.edge_snapshot.active_holds",
                return_value=[
                    {
                        "name": "MANUAL-1",
                        "source": "manual",
                        "reason_code": "manual_manager_pause",
                        "reason_label": "Stock check required",
                        "expires_at": None,
                        "stale": False,
                        "pos_profile": "Outlet POS",
                    },
                    {
                        "name": "MANUAL-2",
                        "source": "manual",
                        "reason_code": "director_hold",
                        "reason_label": "Director hold",
                        "expires_at": None,
                        "stale": False,
                        "pos_profile": "Outlet POS",
                    },
                ],
            ),
        ):
            holds = _safe_holds(
                target_type="Item",
                target_id="MONT-BLANC",
                warehouse="Outlet - J",
                pos_profile="Outlet POS",
            )
        self.assertTrue(holds[0]["manager_owned"])
        self.assertFalse(holds[1]["manager_owned"])


if __name__ == "__main__":
    unittest.main()
