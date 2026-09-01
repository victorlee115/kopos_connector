from __future__ import annotations

import unittest
from contextlib import nullcontext
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

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

    def test_count_claim_lock_serializes_competing_tablets(self) -> None:
        with patch.object(inventory.frappe.db, "sql") as sql:
            inventory._lock_count_task("COUNT-A")

        query = sql.call_args.args[0]
        self.assertIn("tabFB Inventory Count Task", query)
        self.assertIn("FOR UPDATE", query)
        self.assertEqual(sql.call_args.args[1], ("COUNT-A",))

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

    def test_guided_revision_accepts_only_the_equal_legacy_db_timestamp(self) -> None:
        document = {"modified": "2026-08-15 10:00:00.123456"}

        inventory._validate_guided_source_revision(
            {"source_revision": "2026-08-15 10:00:00.123456"},
            document,
            "BOM",
        )
        inventory._validate_guided_source_revision(
            {"source_revision": "2026-08-15T10:00:00.123456+08:00"},
            document,
            "BOM",
        )
        with self.assertRaisesRegex(frappe.ValidationError, "changed after"):
            inventory._validate_guided_source_revision(
                {"source_revision": "2026-08-15 10:00:01.123456"},
                document,
                "BOM",
            )
        with self.assertRaisesRegex(frappe.ValidationError, "changed after"):
            inventory._validate_guided_source_revision(
                {"source_revision": "2026-08-15"},
                document,
                "BOM",
            )

    def test_preparation_start_uses_direct_outlet_backflush(self) -> None:
        document = SimpleNamespace(
            doctype="Work Order",
            name="MFG-WO-0001",
            docstatus=0,
            skip_transfer=0,
            flags=SimpleNamespace(ignore_permissions=False),
            submit=Mock(),
        )
        meta = SimpleNamespace(has_field=lambda field: field in {
            "skip_transfer",
            "custom_kopos_inventory_command_id",
            "custom_kopos_inventory_payload_hash",
            "custom_kopos_inventory_recorded_by",
            "custom_kopos_inventory_confirmed_by",
        })
        value = {
            "work_order": document.name,
            "payload_hash": "a" * 64,
            "recorded_by": "staff@example.com",
            "staff_user": "manager@example.com",
        }
        with patch.object(inventory, "_lock_guided_source_document"), patch.object(
            inventory.frappe, "get_doc", return_value=document
        ), patch.object(inventory, "_validate_work_order_scope"), patch.object(
            inventory, "_validate_guided_source_revision"
        ), patch.object(inventory.frappe, "get_meta", return_value=meta):
            result = inventory._submit_work_order(value, "command-1")

        self.assertEqual(result, document.name)
        self.assertEqual(document.skip_transfer, 1)
        self.assertTrue(document.flags.ignore_permissions)
        document.submit.assert_called_once_with()

    def test_guided_stock_documents_run_as_the_dedicated_automation_user(self) -> None:
        value = {
            "command_id": "receipt-1",
            "payload_hash": "a" * 64,
            "task_type": "submit_purchase_receipt",
            "device_config_version": 7,
            "company": "JiJi Foods",
            "warehouse": "Outlet - WH",
            "source_document": "PO-1",
            "purchase_order": "PO-1",
        }
        command_receipt = SimpleNamespace(
            task_type="submit_purchase_receipt",
            outcome="",
            result_doctype="",
            result_document="",
        )

        def complete_receipt(receipt, *, result_doctype, result_document, outcome):
            receipt.outcome = outcome
            receipt.result_doctype = result_doctype
            receipt.result_document = result_document

        with patch.object(inventory, "require_device_context", return_value=SimpleNamespace()), patch.object(
            inventory, "_parse_guided_task_payload", return_value=value
        ), patch.object(inventory, "_validate_guided_task_actor"), patch.object(
            inventory, "_validate_guided_task_scope"
        ), patch.object(
            inventory, "_validate_guided_task_actionable_quantities"
        ), patch.object(
            inventory,
            "lock_device_for_operational_mutation",
            return_value=SimpleNamespace(config_version=7),
        ), patch.object(
            inventory, "_get_guided_command_receipt", return_value=None
        ), patch.object(
            inventory, "_find_command_document", return_value=None
        ), patch.object(
            inventory, "_create_guided_command_receipt", return_value=command_receipt
        ), patch.object(
            inventory, "_guided_result_doctype", return_value="Purchase Receipt"
        ), patch.object(
            inventory, "_complete_guided_command_receipt", side_effect=complete_receipt
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

    def test_staff_waste_submits_under_the_automation_user_without_review(self) -> None:
        value = {
            "command_id": "inventory-waste:WASTE-1",
            "payload_hash": "c" * 64,
            "task_type": "record_waste_event",
            "device_config_version": 7,
            "company": "JiJi Foods",
            "warehouse": "Outlet - WH",
            "source_document": "WASTE-1",
            "waste_id": "WASTE-1",
        }
        command_receipt = SimpleNamespace(
            task_type="record_waste_event",
            outcome="",
            result_doctype="",
            result_document="",
        )

        def complete_receipt(receipt, *, result_doctype, result_document, outcome):
            receipt.outcome = outcome
            receipt.result_doctype = result_doctype
            receipt.result_document = result_document

        with patch.object(inventory, "require_device_context", return_value=SimpleNamespace()), patch.object(
            inventory, "_parse_guided_task_payload", return_value=value
        ), patch.object(inventory, "_validate_guided_task_actor"), patch.object(
            inventory, "_validate_guided_task_scope"
        ), patch.object(
            inventory, "_validate_guided_task_actionable_quantities"
        ), patch.object(
            inventory,
            "lock_device_for_operational_mutation",
            return_value=SimpleNamespace(config_version=7),
        ), patch.object(
            inventory, "_get_guided_command_receipt", return_value=None
        ), patch.object(
            inventory, "_find_command_document", return_value=None
        ), patch.object(
            inventory, "_create_guided_command_receipt", return_value=command_receipt
        ), patch.object(
            inventory, "_guided_result_doctype", return_value="FB Waste Event"
        ), patch.object(
            inventory, "_complete_guided_command_receipt", side_effect=complete_receipt
        ), patch.object(
            inventory, "inventory_automation_identity", return_value=nullcontext("automation@example.com")
        ) as identity, patch.object(
            inventory, "_record_waste_event", return_value="FB-WASTE-0001"
        ) as record_waste, patch.object(inventory.frappe.db, "commit"):
            result = inventory._handle_guided_task(
                device_id="TAB-A",
                payload={},
                task_type="record_waste_event",
            )

        identity.assert_called_once_with(
            company="JiJi Foods",
            warehouse="Outlet - WH",
            create_doctypes=("Stock Entry",),
            submit_doctypes=("Stock Entry",),
        )
        record_waste.assert_called_once_with(value, "inventory-waste:WASTE-1")
        self.assertEqual(result["document"], "FB-WASTE-0001")
        self.assertEqual(result["outcome"], "completed")

    def test_made_product_waste_ignores_client_lines_and_saves_resolved_provenance(self) -> None:
        class WasteDocument:
            doctype = "FB Waste Event"
            name = "FB-WASTE-0002"

            def __init__(self):
                self.lines = []

            def append(self, fieldname, value):
                self.assert_field(fieldname)
                self.lines.append(SimpleNamespace(**value))

            @staticmethod
            def assert_field(fieldname):
                if fieldname != "lines":
                    raise AssertionError(fieldname)

            def insert(self, **_kwargs):
                return None

        document = WasteDocument()
        value = {
            "company": "Moonleaf Café",
            "warehouse": "Moonleaf Bar - MC",
            "waste_id": "WASTE-MADE-1",
            "reason_code": "Prep Error",
            "payload_hash": "d" * 64,
            "source_type": "made_item",
            "source_item": "MATCHA-LATTE",
            "source_quantity": "2",
            "selected_modifiers": ["OAT-MILK"],
            # This must never be accepted as authority for a made product.
            "lines": [{"item": "EXPENSIVE-OTHER-CAFE-ITEM", "qty": "999"}],
        }
        resolved = {
            "recipe": "MATCHA-RECIPE-V3",
            "recipe_hash": "e" * 64,
            "selected_modifiers": ["OAT-MILK"],
            "lines": [
                {"item": "MATCHA", "qty_decimal": "8", "uom": "Gram"},
                {"item": "OAT-MILK", "qty_decimal": "360", "uom": "Millilitre"},
            ],
        }
        with patch.object(
            inventory, "resolve_made_item_waste", return_value=resolved
        ), patch.object(
            inventory.frappe, "new_doc", return_value=document
        ), patch.object(
            inventory, "_set_document_value"
        ), patch.object(
            inventory, "_apply_guided_task_audit"
        ), patch.object(
            inventory, "_submit_waste_document_now", return_value=document.name
        ):
            result = inventory._record_waste_event(value, "inventory-waste:WASTE-MADE-1")

        self.assertEqual(result, "FB-WASTE-0002")
        self.assertEqual([line.item for line in document.lines], ["MATCHA", "OAT-MILK"])
        self.assertEqual([line.qty_decimal for line in document.lines], ["8", "360"])
        self.assertEqual(document.source_type, "Made Item")
        self.assertEqual(document.recipe, "MATCHA-RECIPE-V3")
        self.assertEqual(document.recipe_canonical_hash, "e" * 64)
        self.assertEqual(json.loads(document.selected_modifiers_json), ["OAT-MILK"])
        self.assertEqual(
            json.loads(document.resolved_lines_json),
            [
                {"item": "MATCHA", "qty": "8", "uom": "Gram"},
                {"item": "OAT-MILK", "qty": "360", "uom": "Millilitre"},
            ],
        )

    def test_guided_stock_document_refuses_an_unsafe_automation_identity(self) -> None:
        value = {
            "command_id": "transfer-1",
            "payload_hash": "b" * 64,
            "task_type": "submit_transfer_dispatch",
            "device_config_version": 7,
            "company": "JiJi Foods",
            "warehouse": "Outlet - WH",
            "source_document": "MR-1",
            "material_request": "MR-1",
        }
        with patch.object(inventory, "require_device_context", return_value=SimpleNamespace()), patch.object(
            inventory, "_parse_guided_task_payload", return_value=value
        ), patch.object(inventory, "_validate_guided_task_actor"), patch.object(
            inventory, "_validate_guided_task_scope"
        ), patch.object(
            inventory, "_validate_guided_task_actionable_quantities"
        ), patch.object(
            inventory,
            "lock_device_for_operational_mutation",
            return_value=SimpleNamespace(config_version=7),
        ), patch.object(
            inventory, "_get_guided_command_receipt", return_value=None
        ), patch.object(
            inventory, "_find_command_document", return_value=None
        ), patch.object(
            inventory, "_create_guided_command_receipt", return_value=SimpleNamespace()
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

    def test_guided_receiving_requires_a_real_amount_or_discrepancy(self) -> None:
        with self.assertRaisesRegex(frappe.ValidationError, "at least one received"):
            inventory._validate_guided_task_actionable_quantities(
                {
                    "lines": [{
                        "qty": "0",
                        "missing_qty": "0",
                        "damaged_qty": "0",
                        "excess_qty": "0",
                    }],
                },
                task_type="submit_purchase_receipt",
            )

        inventory._validate_guided_task_actionable_quantities(
            {
                "lines": [{
                    "qty": "0",
                    "missing_qty": "1",
                    "damaged_qty": "0",
                    "excess_qty": "0",
                }],
            },
            task_type="submit_purchase_receipt",
        )

    def test_guided_transfer_requires_every_line_to_move_stock(self) -> None:
        with self.assertRaisesRegex(frappe.ValidationError, "Every transfer amount"):
            inventory._validate_guided_task_actionable_quantities(
                {"lines": [{"qty": "2"}, {"qty": "0"}]},
                task_type="submit_transfer_dispatch",
            )

    def test_open_delivery_difference_pauses_only_the_exact_outlet_order(self) -> None:
        with patch.object(inventory.frappe.db, "exists", return_value=True), patch.object(
            inventory.frappe.db,
            "get_value",
            return_value="INV-EX-1",
        ) as get_value:
            self.assertTrue(inventory._supplier_receiving_is_paused(
                company="JiJi Foods",
                warehouse="Outlet - WH",
                purchase_order="PO-1",
            ))

        self.assertEqual(get_value.call_args.args[0], "FB Inventory Exception")
        self.assertEqual(get_value.call_args.args[1], {
            "reason_code": "supplier_receiving_discrepancy",
            "status": "Open",
            "company": "JiJi Foods",
            "warehouse": "Outlet - WH",
            "source_doctype": "Purchase Order",
            "source_name": "PO-1",
        })

    def test_guided_command_replay_rejects_reused_identity(self) -> None:
        value = {
            "command_id": "receive-1",
            "payload_hash": "a" * 64,
            "task_type": "submit_purchase_receipt",
            "device_id": "TAB-A",
            "company": "JiJi Foods",
            "warehouse": "Outlet - WH",
            "source_document": "PO-1",
            "source_revision": "2026-08-15T10:00:00+08:00",
        }
        receipt = dict(value, state="Completed", outcome="Completed", result_doctype="Purchase Receipt", result_document="PRE-1")
        receipt["payload_hash"] = "b" * 64
        with patch.object(inventory.frappe.db, "exists", return_value=True), patch.object(
            inventory.frappe.db, "get_value", return_value=receipt
        ):
            with self.assertRaisesRegex(frappe.ValidationError, "reused with different content"):
                inventory._get_guided_command_receipt(value)

    def test_guided_task_rejects_a_stale_locked_device_configuration(self) -> None:
        value = {
            "command_id": "receipt-stale",
            "payload_hash": "c" * 64,
            "device_config_version": 6,
            "company": "JiJi Foods",
            "warehouse": "Outlet - WH",
            "source_document": "PO-1",
            "purchase_order": "PO-1",
        }
        with patch.object(inventory, "require_device_context"), patch.object(
            inventory, "_parse_guided_task_payload", return_value=value
        ), patch.object(
            inventory,
            "lock_device_for_operational_mutation",
            return_value=SimpleNamespace(config_version=7),
        ), patch.object(
            inventory, "_validate_guided_task_actor"
        ) as validate_actor, patch.object(
            inventory, "_execute_guided_task_mutation"
        ) as mutate:
            with self.assertRaisesRegex(frappe.ValidationError, "configuration changed"):
                inventory._handle_guided_task(
                    device_id="TAB-A",
                    payload={},
                    task_type="submit_purchase_receipt",
                )

        validate_actor.assert_not_called()
        mutate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
