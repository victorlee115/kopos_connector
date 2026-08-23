from __future__ import annotations

import unittest
from contextlib import nullcontext
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import patch

from kopos_connector.tests.fake_frappe import install_fake_frappe_modules


install_fake_frappe_modules()

import frappe

from kopos_connector.api import inventory


class InventoryDeviceReportContractTests(unittest.TestCase):
    def _payload(self) -> dict[str, object]:
        return {
            "schema_version": "inventory-device-state-v2",
            "device_id": "TAB-A",
            "config_version": 7,
            "report_revision": 4,
            "observed_at": "2026-08-15T10:00:00+08:00",
            "catalog_version": "sha256:catalog-1",
            "overlay_version": "overlay-1",
            "overlay_hash": "overlay-1",
            "sales_outbox": {"pending": 0, "syncing": 0, "failed": 0, "dead_letter": 0},
            "inventory_outbox": {"pending": 1, "syncing": 0, "failed": 0, "dead_letter": 0},
            "oldest_unsaved_sale_timestamp": None,
        }

    def test_report_requires_catalog_and_all_inventory_queue_states(self) -> None:
        parsed = inventory._parse_report_payload(self._payload())
        self.assertEqual(parsed["catalog_version"], "sha256:catalog-1")
        self.assertEqual(set(parsed["inventory_outbox"]), {"pending", "syncing", "failed", "dead_letter"})

        missing_catalog = deepcopy(self._payload())
        missing_catalog.pop("catalog_version")
        with self.assertRaises(frappe.ValidationError):
            inventory._parse_report_payload(missing_catalog)

        missing_dead_letter = deepcopy(self._payload())
        missing_dead_letter["inventory_outbox"] = {"pending": 0, "failed": 0}
        with self.assertRaises(frappe.ValidationError):
            inventory._parse_report_payload(missing_dead_letter)

        missing_offset = deepcopy(self._payload())
        missing_offset["observed_at"] = "2026-08-15T10:00:00"
        with self.assertRaisesRegex(frappe.ValidationError, "explicit UTC offset"):
            inventory._parse_report_payload(missing_offset)

    def test_device_payload_limits_apply_to_canonical_dicts_and_raw_json(self) -> None:
        oversized = deepcopy(self._payload())
        oversized["catalog_version"] = "x" * (inventory._DEVICE_MAX_PAYLOAD_BYTES + 1)
        with self.assertRaisesRegex(frappe.ValidationError, "too large"):
            inventory._parse_report_payload(oversized)

        raw = json.dumps(self._payload(), separators=(",", ":"))
        with self.assertRaisesRegex(frappe.ValidationError, "too large"):
            inventory._parse_report_payload(raw + (" " * inventory._DEVICE_MAX_PAYLOAD_BYTES))

    def test_device_payload_limits_reject_depth_and_unsafe_numbers(self) -> None:
        nested: object = "leaf"
        for _ in range(inventory._DEVICE_MAX_DEPTH + 1):
            nested = [nested]
        with self.assertRaisesRegex(frappe.ValidationError, "nested deeper"):
            inventory._parse_device_json_object({"nested": nested}, "Device payload")

        with self.assertRaisesRegex(frappe.ValidationError, "safe JSON integer"):
            inventory._parse_device_json_object(
                {"number": inventory._DEVICE_MAX_SAFE_INTEGER + 1}, "Device payload"
            )

    def test_count_observation_line_count_is_bounded_before_domain_validation(self) -> None:
        with self.assertRaisesRegex(frappe.ValidationError, "more than 100"):
            inventory._parse_count_payload(
                {
                    "schema_version": "inventory-command-v1",
                    "lines": [{}] * (inventory._DEVICE_MAX_LINES + 1),
                }
            )

    def test_device_current_helper_requires_exact_catalog_and_overlay_identity(self) -> None:
        row = {
            "name": "TAB-A",
            "inventory_catalog_version": "sha256:catalog-1",
            "inventory_overlay_version": "overlay-1",
            "inventory_overlay_hash": "overlay-1",
        }
        cache = type(
            "Cache",
            (),
            {"get_value": lambda _self, _key: '{"catalog_version":"sha256:catalog-1","version":"overlay-1","overlay_hash":"overlay-1"}'},
        )()
        with patch.object(inventory.frappe, "cache", return_value=cache):
            self.assertTrue(inventory._device_overlay_current(row))
            mismatched = dict(row, inventory_catalog_version="sha256:catalog-2")
            self.assertFalse(inventory._device_overlay_current(mismatched))

    def test_pos_hold_target_guard_rejects_pure_ingredients(self) -> None:
        profile = {"company": "JiJi Foods", "name": "Outlet POS"}
        with patch.object(inventory, "get_catalog_target_ids", return_value=({"MONT-BLANC"}, {"MOD-COLD-FOAM"})):
            inventory._assert_catalog_hold_target(
                profile=profile,
                target_type="Item",
                target_id="MONT-BLANC",
            )
            inventory._assert_catalog_hold_target(
                profile=profile,
                target_type="Modifier",
                target_id="MOD-COLD-FOAM",
            )
            with self.assertRaises(frappe.PermissionError):
                inventory._assert_catalog_hold_target(
                    profile=profile,
                    target_type="Item",
                    target_id="COLD-FOAM",
                )

    def test_hold_command_parser_rejects_missing_or_tampered_envelope(self) -> None:
        valid = {
            "schema_version": "inventory-command-v1",
            "command_id": "hold-1",
            "action": "create",
            "device_id": "TAB-A",
            "device_config_version": 7,
            "staff_user": "manager@example.com",
            "employee": "EMP-1",
            "company": "JiJi Foods",
            "outlet": "Outlet POS",
            "warehouse": "Outlet - WH",
            "source_document": "catalog-1",
            "source_revision": 1,
            "observed_at": "2026-08-15T10:00:00+08:00",
            "target_type": "Item",
            "target_id": "MONT-BLANC",
            "reason_code": "manual_manager_pause",
            "reason_label": "Stock check required",
            "payload_hash": "0" * 64,
        }
        with self.assertRaises(frappe.ValidationError):
            inventory._parse_availability_hold_command(valid, action="create")
        missing = dict(valid)
        missing.pop("employee")
        with self.assertRaises(frappe.ValidationError):
            inventory._parse_availability_hold_command(missing, action="create")

    def test_hold_command_parser_accepts_the_overlay_hash_as_source_revision(self) -> None:
        valid = {
            "schema_version": "inventory-command-v1",
            "command_id": "hold-overlay-1",
            "action": "create",
            "device_id": "TAB-A",
            "device_config_version": 7,
            "staff_user": "manager@example.com",
            "employee": "EMP-1",
            "company": "JiJi Foods",
            "outlet": "Outlet POS",
            "warehouse": "Outlet - WH",
            "source_document": "overlay-version-9",
            "source_revision": "f" * 64,
            "observed_at": "2026-08-15T10:00:00+08:00",
            "target_type": "Item",
            "target_id": "MONT-BLANC",
            "reason_code": "manual_manager_pause",
            "reason_label": "Stock check required",
        }
        valid["payload_hash"] = inventory._payload_hash(valid)

        parsed = inventory._parse_availability_hold_command(valid, action="create")

        self.assertEqual(parsed["source_document"], "overlay-version-9")
        self.assertEqual(parsed["source_revision"], "f" * 64)

    def test_guided_task_envelope_requires_schema_revision_and_exact_hash(self) -> None:
        value = {
            "schema_version": "inventory-command-v1",
            "command_id": "guided-1",
            "task_type": "submit_purchase_receipt",
            "device_id": "TAB-A",
            "device_config_version": 7,
            "staff_user": "manager@example.com",
            "employee": "EMP-1",
            "company": "JiJi Foods",
            "outlet": "Outlet POS",
            "warehouse": "Outlet - WH",
            "source_document": "PO-1",
            "source_revision": "2026-08-15T10:00:00+08:00",
            "observed_at": "2026-08-15T10:00:00+08:00",
            "purchase_order": "PO-1",
            "lines": [],
        }
        value["payload_hash"] = inventory._payload_hash(value)
        parsed = inventory._parse_guided_task_payload(
            value, task_type="submit_purchase_receipt", device_id="TAB-A"
        )
        self.assertEqual(parsed["source_revision"], "2026-08-15T10:00:00+08:00")

        tampered = dict(value, warehouse="Other - WH")
        with self.assertRaises(frappe.ValidationError):
            inventory._parse_guided_task_payload(
                tampered, task_type="submit_purchase_receipt", device_id="TAB-A"
            )
        missing_schema = dict(value)
        missing_schema.pop("schema_version")
        with self.assertRaises(frappe.ValidationError):
            inventory._parse_guided_task_payload(
                missing_schema, task_type="submit_purchase_receipt", device_id="TAB-A"
            )

    def test_guided_task_request_is_bounded_by_size_lines_and_line_text(self) -> None:
        base = {
            "schema_version": "inventory-command-v1",
            "command_id": "guided-1",
            "task_type": "submit_purchase_receipt",
            "device_id": "TAB-A",
            "device_config_version": 7,
            "staff_user": "manager@example.com",
            "employee": "EMP-1",
            "company": "JiJi Foods",
            "outlet": "Outlet POS",
            "warehouse": "Outlet - WH",
            "source_document": "PO-1",
            "source_revision": "2026-08-15T10:00:00+08:00",
            "observed_at": "2026-08-15T10:00:00+08:00",
            "purchase_order": "PO-1",
            "lines": [],
        }

        too_many = dict(base, lines=[{"item_code": "MILK"}] * 101)
        too_many["payload_hash"] = inventory._payload_hash(too_many)
        with self.assertRaisesRegex(frappe.ValidationError, "more than 100"):
            inventory._parse_guided_task_payload(
                too_many, task_type="submit_purchase_receipt", device_id="TAB-A"
            )

        too_long_line = dict(base, lines=[{"item_code": "M" * 141}])
        too_long_line["payload_hash"] = inventory._payload_hash(too_long_line)
        with self.assertRaisesRegex(frappe.ValidationError, "field item_code is too long"):
            inventory._parse_guided_task_payload(
                too_long_line, task_type="submit_purchase_receipt", device_id="TAB-A"
            )

        too_large = dict(base, command_id="x" * 161)
        too_large["payload_hash"] = inventory._payload_hash(too_large)
        with self.assertRaisesRegex(frappe.ValidationError, "command_id is too long"):
            inventory._parse_guided_task_payload(
                too_large, task_type="submit_purchase_receipt", device_id="TAB-A"
            )

    def test_standard_document_revision_is_derived_from_modified_timestamp(self) -> None:
        revision = inventory._authoritative_document_revision({"modified": "2026-08-15 10:00:00"})
        self.assertEqual(revision, "2026-08-15T10:00:00+08:00")
        with self.assertRaises(ValueError):
            inventory._authoritative_document_revision({})

    def test_guided_stock_documents_run_as_the_dedicated_automation_user(self) -> None:
        value = {
            "command_id": "receipt-1",
            "payload_hash": "a" * 64,
            "company": "JiJi Foods",
            "warehouse": "Outlet - WH",
            "source_document": "PO-1",
            "purchase_order": "PO-1",
        }
        with patch.object(inventory, "require_device_context", return_value=SimpleNamespace()), patch.object(
            inventory, "_parse_guided_task_payload", return_value=value
        ), patch.object(inventory, "_validate_guided_task_actor"), patch.object(
            inventory, "_validate_guided_task_scope"
        ), patch.object(inventory, "lock_device_for_operational_mutation"), patch.object(
            inventory, "_find_command_document", return_value=None
        ), patch.object(
            inventory, "inventory_automation_identity", return_value=nullcontext("automation@example.com")
        ) as identity, patch.object(
            inventory, "_create_purchase_receipt", return_value="MAT-PRE-0001"
        ) as create_receipt, patch.object(inventory.frappe.db, "commit"):
            result = inventory._handle_guided_task(
                device_id="TAB-A",
                payload={},
                task_type="submit_purchase_receipt",
            )

        identity.assert_called_once_with(
            company="JiJi Foods",
            warehouse="Outlet - WH",
            create_doctypes=("Purchase Receipt",),
            submit_doctypes=("Purchase Receipt",),
        )
        create_receipt.assert_called_once_with(value, "receipt-1")
        self.assertEqual(result["document"], "MAT-PRE-0001")

    def test_guided_stock_document_refuses_an_unsafe_automation_identity(self) -> None:
        value = {
            "command_id": "transfer-1",
            "payload_hash": "b" * 64,
            "company": "JiJi Foods",
            "warehouse": "Outlet - WH",
            "source_document": "MR-1",
            "material_request": "MR-1",
        }
        with patch.object(inventory, "require_device_context", return_value=SimpleNamespace()), patch.object(
            inventory, "_parse_guided_task_payload", return_value=value
        ), patch.object(inventory, "_validate_guided_task_actor"), patch.object(
            inventory, "_validate_guided_task_scope"
        ), patch.object(inventory, "lock_device_for_operational_mutation"), patch.object(
            inventory, "_find_command_document", return_value=None
        ), patch.object(
            inventory,
            "inventory_automation_identity",
            side_effect=inventory.AutomationIdentityError("Automation User lacks submit permission for Stock Entry"),
        ), patch.object(inventory, "_create_transfer_entry") as create_transfer:
            with self.assertRaisesRegex(
                frappe.ValidationError,
                "Guided inventory automation is not configured safely",
            ):
                inventory._handle_guided_task(
                    device_id="TAB-A",
                    payload={},
                    task_type="submit_transfer_dispatch",
                )

        create_transfer.assert_not_called()


if __name__ == "__main__":
    unittest.main()
