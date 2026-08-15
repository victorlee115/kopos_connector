from __future__ import annotations

import ast
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

from kopos_connector.tests.fake_frappe import install_fake_frappe_modules


install_fake_frappe_modules()

import frappe

from kopos_connector.api import inventory


class InventoryCountConfirmationTests(unittest.TestCase):
    def test_device_lock_is_used_as_a_transaction_lock_not_a_context_manager(self) -> None:
        source_path = Path(inventory.__file__)
        tree = ast.parse(source_path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.With):
                continue
            for item in node.items:
                expression = item.context_expr
                if isinstance(expression, ast.Call) and isinstance(expression.func, ast.Name):
                    self.assertNotEqual(
                        expression.func.id,
                        "lock_device_for_operational_mutation",
                        "the device row lock returns a document and is held until transaction end",
                    )

    def _payload(self) -> dict[str, object]:
        payload = {
            "schema_version": "inventory-command-v1",
            "command_id": "inventory-count-confirm:OBS-1:3",
            "task_type": "confirm_count_reconciliation",
            "device_id": "TAB-A",
            "device_config_version": 7,
            "staff_user": "staff@example.com",
            "observation_id": "OBS-1",
            "task_id": "TASK-1",
            "task_revision": 3,
            "warehouse": "Outlet - A",
            "employee": "EMP-1",
            "company": "JiJi Sdn Bhd",
            "outlet": "POS-Outlet-A",
            "source_document": "TASK-1",
            "source_revision": 3,
            "observed_at": "2026-08-15T10:00:00+08:00",
            "manager_approval_token": "token",
        }
        payload["payload_hash"] = inventory._payload_hash(payload)
        return payload

    def test_parser_accepts_the_device_contract_without_financial_fields(self) -> None:
        parsed = inventory._parse_count_confirmation_payload(self._payload())
        self.assertEqual(parsed["staff_user"], "staff@example.com")
        self.assertEqual(parsed["task_revision"], 3)

    def test_parser_rejects_a_timestamp_without_an_explicit_offset(self) -> None:
        payload = self._payload()
        payload["observed_at"] = "2026-08-15T10:00:00"
        payload["payload_hash"] = inventory._payload_hash(
            {key: value for key, value in payload.items() if key != "payload_hash"}
        )
        with self.assertRaisesRegex(frappe.ValidationError, "explicit UTC offset"):
            inventory._parse_count_confirmation_payload(payload)

    def test_safe_observation_creates_draft_but_waits_for_manager_confirmation(self) -> None:
        value = {
            "schema_version": "inventory-command-v1",
            "command_id": "inventory-count-observation:TASK-1:3",
            "task_type": "submit_count_observation",
            "device_id": "TAB-A",
            "device_config_version": 7,
            "staff_user": "staff@example.com",
            "employee": "EMP-1",
            "company": "JiJi",
            "outlet": "POS-Outlet-A",
            "source_document": "TASK-1",
            "source_revision": 3,
            "payload_hash": "hash-from-parser-test",
            "observation_id": "OBS-1",
            "task_id": "TASK-1",
            "task_revision": 3,
            "warehouse": "Outlet - A",
            "actor_id": "staff@example.com",
            "stock_watermark": "2026-08-15T02:00:00+08:00",
            "observed_at": "2026-08-15T10:00:00+08:00",
            "lines": [{"item_id": "MILK", "quantity": "8.5", "uom": "L"}],
        }

        class FakeDocument:
            def __init__(self, name: str) -> None:
                self.name = name
                self.flags = SimpleNamespace(ignore_permissions=False)
                self.items: list[dict[str, object]] = []
                self.status = None

            def append(self, fieldname: str, value: dict[str, object]) -> None:
                assert fieldname == "items"
                self.items.append(value)

            def insert(self, **_kwargs: object) -> None:
                return None

            def save(self, **_kwargs: object) -> None:
                return None

        observation_document = FakeDocument("OBS-ROW-1")
        reconciliation_document = FakeDocument("KOPOS-COUNT-RECON")

        def exists(doctype: str, name: object = None, **_kwargs: object) -> bool:
            if doctype == "DocType":
                return name == "FB Inventory Count Observation"
            if doctype == "FB Inventory Count Task":
                return True
            return False

        with patch.object(inventory, "require_device_context", return_value=SimpleNamespace(api_user="staff@example.com", config_version=7)), patch.object(
            inventory, "_parse_count_payload", return_value=value
        ), patch.object(
            inventory, "resolve_catalog_pos_profile", return_value={"name": "POS-Outlet-A", "warehouse": "Outlet - A", "company": "JiJi"}
        ), patch.object(
            inventory, "lock_device_for_operational_mutation", return_value=SimpleNamespace(api_user="staff@example.com", config_version=7)
        ), patch.object(
            inventory, "_validate_count_assignment", return_value={"employee": "EMP-1"}
        ), patch.object(
            inventory, "_count_variance", return_value={"percent": 0, "value": 0, "valuation_complete": 1}
        ), patch.object(inventory, "_count_requires_director_review", return_value=False), patch.object(
            inventory, "_count_review_projection", return_value={"observation_id": "OBS-1", "status": "review_required", "lines": []}
        ), patch.object(
            inventory.frappe.db, "exists", side_effect=exists
        ), patch.object(
            inventory.frappe.db, "get_value", return_value=None
        ), patch.object(
            inventory.frappe.db, "sql", return_value=[["2026-08-15T02:00:00+08:00"]]
        ), patch.object(
            inventory.frappe, "get_meta", return_value=SimpleNamespace(has_field=lambda _field: True)
        ), patch.object(
            inventory.frappe, "new_doc", side_effect=[observation_document, reconciliation_document]
        ), patch.object(inventory.frappe.db, "set_value"), patch.object(inventory.frappe.db, "commit"):
            result = inventory.submit_count_observation(device_id="TAB-A", payload=value)

        self.assertEqual(result["status"], "review_required")
        self.assertTrue(result["reconciliation"].startswith("KOPOS-COUNT-"))
        self.assertEqual(observation_document.status, "Review")
        self.assertEqual(reconciliation_document.items[0]["uom"], "L")

    def test_parser_rejects_financial_or_expected_quantity_fields(self) -> None:
        payload = self._payload()
        payload["expected_quantity"] = 10
        with self.assertRaisesRegex(frappe.ValidationError, "unsupported fields"):
            inventory._parse_count_confirmation_payload(payload)

    def test_count_observation_envelope_recomputes_pack_total_from_frozen_task(self) -> None:
        payload_without_hash = {
            "schema_version": "inventory-command-v1",
            "command_id": "inventory-count-observation:TASK-1:3",
            "task_type": "submit_count_observation",
            "device_id": "TAB-A",
            "device_config_version": 7,
            "staff_user": "staff@example.com",
            "employee": "EMP-1",
            "company": "JiJi Sdn Bhd",
            "outlet": "POS-Outlet-A",
            "warehouse": "Outlet - A",
            "source_document": "TASK-1",
            "source_revision": 3,
            "observation_id": "inventory-count-observation:TASK-1:3",
            "task_id": "TASK-1",
            "task_revision": 3,
            "actor_id": "staff@example.com",
            "stock_watermark": "2026-08-15T02:00:00+08:00",
            "observed_at": "2026-08-15T10:00:00+08:00",
            "lines": [{
                "item_id": "MILK",
                "stock_uom": "Litre",
                "purchase_uom": "Carton",
                "conversion_factor": "12.5",
                "full_packs": "2",
                "loose_quantity": "0.25",
                "total_quantity": "25.25",
            }],
        }
        payload = {
            **payload_without_hash,
            "payload_hash": inventory._payload_hash(payload_without_hash),
        }
        parsed = inventory._parse_count_payload(payload)
        normalized = inventory._normalize_count_observation_lines(
            [SimpleNamespace(item_id="MILK", uom="Litre", stock_uom="Litre", purchase_uom="Carton", conversion_factor="12.5")],
            parsed["lines"],
        )
        self.assertEqual(normalized[0]["quantity"], "25.25")
        self.assertEqual(normalized[0]["full_packs"], "2")
        self.assertEqual(normalized[0]["loose_quantity"], "0.25")

    def test_count_observation_rejects_client_total_that_does_not_match_pack_breakdown(self) -> None:
        payload_without_hash = {
            "schema_version": "inventory-command-v1",
            "command_id": "inventory-count-observation:TASK-1:3",
            "task_type": "submit_count_observation",
            "device_id": "TAB-A",
            "device_config_version": 7,
            "staff_user": "staff@example.com",
            "employee": "EMP-1",
            "company": "JiJi Sdn Bhd",
            "outlet": "POS-Outlet-A",
            "warehouse": "Outlet - A",
            "source_document": "TASK-1",
            "source_revision": 3,
            "observation_id": "inventory-count-observation:TASK-1:3",
            "task_id": "TASK-1",
            "task_revision": 3,
            "actor_id": "staff@example.com",
            "stock_watermark": "watermark",
            "observed_at": "2026-08-15T10:00:00+08:00",
            "lines": [{
                "item_id": "MILK",
                "stock_uom": "Litre",
                "purchase_uom": "Carton",
                "conversion_factor": "12.5",
                "full_packs": "2",
                "loose_quantity": "0.25",
                "total_quantity": "25",
            }],
        }
        payload = {**payload_without_hash, "payload_hash": inventory._payload_hash(payload_without_hash)}
        parsed = inventory._parse_count_payload(payload)
        with self.assertRaisesRegex(frappe.ValidationError, "does not match"):
            inventory._normalize_count_observation_lines(
                [SimpleNamespace(item_id="MILK", uom="Litre", stock_uom="Litre", purchase_uom="Carton", conversion_factor="12.5")],
                parsed["lines"],
            )

    def test_parser_requires_recorded_employee_and_manager_proof(self) -> None:
        payload = self._payload()
        payload.pop("employee")
        with self.assertRaisesRegex(frappe.ValidationError, "employee is required"):
            inventory._parse_count_confirmation_payload(payload)

        payload = self._payload()
        payload.pop("manager_approval_token")
        with self.assertRaisesRegex(frappe.ValidationError, "manager_approval_token is required"):
            inventory._parse_count_confirmation_payload(payload)

    def test_line_variance_is_signed_and_does_not_return_expected_qty(self) -> None:
        with patch.object(inventory.frappe.db, "get_value", return_value="10"):
            variance = inventory._count_line_variance_percent(
                warehouse="Outlet - A",
                item_id="MILK",
                counted_quantity="8.5",
            )
        self.assertEqual(variance, "-15")

    def test_review_projection_is_operational_only(self) -> None:
        observation = {
            "name": "OBS-ROW-1",
            "observation_id": "OBS-1",
            "status": "Accepted",
            "lines_json": '[{"item_id":"MILK","quantity":"8.5","uom":"L"}]',
            "reconciliation": "MAT-RECON-1",
        }
        with (
            patch.object(inventory.frappe.db, "exists", return_value=True),
            patch.object(inventory.frappe, "get_all", return_value=[observation]),
            patch.object(inventory.frappe.db, "get_value", return_value="10"),
        ):
            review = inventory._count_review_projection(
                task_name="TASK-1",
                task_revision=3,
                warehouse="Outlet - A",
                assignee="staff@example.com",
            )
        self.assertEqual(
            review,
            {
                "observation_id": "OBS-1",
                "status": "accepted",
                "lines": [{
                    "item_id": "MILK",
                    "uom": "L",
                    "counted_quantity": "8.5",
                    "variance_percent": "-15",
                }],
            },
        )
        serialized = str(review).lower()
        for forbidden in ("expected", "valuation", "cogs", "rate", "threshold", "value"):
            self.assertNotIn(forbidden, serialized)

    def test_count_task_lines_include_server_resolved_item_name_only(self) -> None:
        with patch.object(
            inventory.frappe,
            "get_all",
            side_effect=[
                [{"item_id": "MILK", "uom": "L"}],
                [{"name": "MILK", "item_name": "Whole Milk", "item_code": "MILK"}],
            ],
        ):
            response = inventory._count_task_response({
                "name": "TASK-1",
                "revision": 2,
                "warehouse": "Outlet - A",
                "assignee": "staff@example.com",
                "stock_watermark": "watermark",
            })
        self.assertEqual(response["lines"], [{"item_id": "MILK", "item_name": "Whole Milk", "uom": "L"}])
        self.assertNotIn("expected_quantity", response["lines"][0])
        self.assertNotIn("valuation", response["lines"][0])

    def test_review_status_exposes_manager_ready_gate(self) -> None:
        observation = {
            "name": "OBS-ROW-1",
            "observation_id": "OBS-1",
            "status": "Review",
            "lines_json": '[{"item_id":"MILK","quantity":"8.5","uom":"L"}]',
            "reconciliation": "KOPOS-COUNT-1",
        }
        with patch.object(inventory.frappe.db, "exists", return_value=True), patch.object(
            inventory.frappe, "get_all", return_value=[observation]
        ), patch.object(inventory.frappe.db, "get_value", return_value="10"):
            review = inventory._count_review_projection(
                task_name="TASK-1",
                task_revision=3,
                warehouse="Outlet - A",
                assignee="staff@example.com",
            )
        self.assertIsNotNone(review)
        self.assertEqual(review["status"], "review_required")

    def test_director_gated_review_without_draft_stays_hidden_from_manager(self) -> None:
        observation = {
            "name": "OBS-ROW-1",
            "observation_id": "OBS-1",
            "status": "Review",
            "lines_json": '[{"item_id":"MILK","quantity":"8.5","uom":"L"}]',
            "reconciliation": None,
        }
        with patch.object(inventory.frappe.db, "exists", return_value=True), patch.object(
            inventory.frappe, "get_all", return_value=[observation]
        ), patch.object(inventory.frappe.db, "get_value", return_value="10"):
            review = inventory._count_review_projection(
                task_name="TASK-1",
                task_revision=3,
                warehouse="Outlet - A",
                assignee="staff@example.com",
            )
        self.assertIsNone(review)

    def test_observation_scope_rejects_wrong_user_employee_warehouse_or_revision(self) -> None:
        observation = {
            "observation_id": "OBS-1",
            "task_id": "TASK-1",
            "task_revision": 3,
            "warehouse": "Outlet - A",
            "actor_id": "staff@example.com",
            "employee": "EMP-1",
            "stock_watermark": "2026-08-15T02:00:00+08:00",
            "status": "Accepted",
        }
        checks = (
            {"staff_id": "other@example.com"},
            {"employee": "EMP-2"},
            {"warehouse": "Outlet - B"},
            {"task_revision": 4},
        )
        for overrides in checks:
            values = {
                "observation_id": "OBS-1",
                "task_id": "TASK-1",
                "task_revision": 3,
                "warehouse": "Outlet - A",
                "staff_id": "staff@example.com",
                "employee": "EMP-1",
            }
            values.update(overrides)
            with self.subTest(overrides=overrides), self.assertRaises(frappe.PermissionError if "staff_id" in overrides or "employee" in overrides or "warehouse" in overrides else frappe.ValidationError):
                inventory._validate_count_confirmation_observation(
                    observation=observation,
                    observation_id=values["observation_id"],
                    task_id=values["task_id"],
                    task_revision=values["task_revision"],
                    warehouse=values["warehouse"],
                    staff_id=values["staff_id"],
                    employee=values["employee"],
                )


if __name__ == "__main__":
    unittest.main()
