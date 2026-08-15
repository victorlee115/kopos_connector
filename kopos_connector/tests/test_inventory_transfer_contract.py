from __future__ import annotations

from contextlib import nullcontext
from decimal import Decimal
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from kopos_connector.tests.fake_frappe import install_fake_frappe_modules


install_fake_frappe_modules()

import frappe

from kopos_connector.api import inventory as inventory_api
from kopos_connector.kopos.services.inventory_autopilot import document_coordinator
from kopos_connector.kopos.services.inventory_autopilot.replenishment import ReplenishmentLine


class _MaterialRequest:
    def __init__(self) -> None:
        self.doctype = "Material Request"
        self.name = "MAT-TRANSFER-1"
        self.docstatus = 0
        self.items: list[dict[str, object]] = []

    def append(self, fieldname: str, value: dict[str, object]) -> None:
        assert fieldname == "items"
        self.items.append(value)

    def insert(self, **_kwargs: object) -> None:
        return None


class _StockEntry:
    def __init__(self, name: str) -> None:
        self.doctype = "Stock Entry"
        self.name = name
        self.flags = SimpleNamespace(ignore_permissions=False)
        self.items: list[dict[str, object]] = []

    def append(self, fieldname: str, value: dict[str, object]) -> None:
        assert fieldname == "items"
        self.items.append(value)

    def insert(self, **_kwargs: object) -> None:
        return None

    def submit(self) -> None:
        return None


class InventoryTransferContractTests(TestCase):
    def test_uom_authority_keeps_source_and_stock_quantities_exact(self) -> None:
        authority = inventory_api._guided_uom_authority(
            SimpleNamespace(
                qty=Decimal("2"),
                uom="Pack",
                stock_uom="Each",
                conversion_factor=Decimal("12"),
                stock_qty=Decimal("24"),
            ),
            item_code="MILK",
            label="Material Request line",
        )
        self.assertEqual(authority["qty"], Decimal("2"))
        self.assertEqual(authority["stock_qty"], Decimal("24"))
        self.assertEqual(authority["conversion_factor"], Decimal("12"))
        with self.assertRaises(frappe.ValidationError):
            inventory_api._guided_uom_authority(
                SimpleNamespace(qty=Decimal("2"), uom="Pack", stock_uom="Each"),
                item_code="MILK",
                label="Material Request line",
            )

    def test_transfer_request_uses_standard_source_and_destination_fields(self) -> None:
        plan = {
            "name": "PLAN-1",
            "warehouse": "Destination",
            "gate_results": '{"policy_active": true, "automation_identity": true, "input_hash_match": true, "no_unresolved_count": true, "devices_current": true, "projection_backlog_clear": true, "recipe_uom_complete": true, "forecast_reliable": true, "source_current": true, "quantity_ceiling": true, "value_ceiling": true, "shelf_life_cap": true, "intent_not_open": true}',
            "lines": [{
                "item": "MILK",
                "action": "Transfer",
                "warehouse": "Destination",
                "source_warehouse": "Source",
                "quantity": "5",
                "uom": "Litre",
            }],
        }
        document = _MaterialRequest()
        line = ReplenishmentLine(
            item="MILK",
            warehouse="Destination",
            quantity=Decimal("5"),
            reason="outlet shortfall",
            source_warehouse="Source",
        )
        with patch.object(document_coordinator, "_execution_plan", return_value=(plan, ())), patch.object(
            document_coordinator, "_has_inventory_fingerprint_field", return_value=True
        ), patch.object(document_coordinator, "_find_by_fingerprint", return_value=None), patch.object(
            document_coordinator, "_find_open_material_request_intent", return_value=None
        ), patch.object(document_coordinator, "_record_plan_document"), patch.object(
            document_coordinator.frappe, "new_doc", return_value=document
        ), patch.object(
            document_coordinator.frappe, "get_meta", return_value=SimpleNamespace(has_field=lambda _field: True)
        ), patch.object(
            document_coordinator.frappe.db,
            "get_value",
            side_effect=lambda _doctype, _name, field, **_kwargs: (
                {"company": "Cafe Co", "is_group": 0, "disabled": 0}
                if isinstance(field, list)
                else "Cafe Co"
            ),
        ), patch.object(
            document_coordinator, "inventory_automation_identity", return_value=nullcontext("AUTO")
        ):
            result = document_coordinator.create_and_submit_material_request(
                company="Cafe Co",
                purpose="Material Transfer",
                required_date="2026-08-16",
                lines=(line,),
                plan_hash="plan-hash",
                policy_hash="policy-hash",
                transit_warehouse="Transit",
            )

        self.assertEqual(result["status"], "created")
        self.assertEqual(document.material_request_type, "Material Transfer")
        self.assertEqual(document.items, [{
            "item_code": "MILK",
            "qty": Decimal("5"),
            "warehouse": "Destination",
            "schedule_date": "2026-08-16",
            "uom": "Litre",
            "stock_uom": "Litre",
            "conversion_factor": Decimal("1"),
            "from_warehouse": "Source",
        }])

    def test_transfer_line_without_explicit_source_is_blocked(self) -> None:
        plan = {
            "name": "PLAN-1",
            "warehouse": "Destination",
            "gate_results": '{"policy_active": true, "automation_identity": true, "input_hash_match": true, "no_unresolved_count": true, "devices_current": true, "projection_backlog_clear": true, "recipe_uom_complete": true, "forecast_reliable": true, "source_current": true, "quantity_ceiling": true, "value_ceiling": true, "shelf_life_cap": true, "intent_not_open": true}',
            "lines": [{
                "item": "MILK", "action": "Transfer", "warehouse": "Destination",
                "source_warehouse": "", "quantity": "5", "uom": "Litre",
            }],
        }
        line = ReplenishmentLine("MILK", "Destination", Decimal("5"), "shortfall")
        with patch.object(document_coordinator, "_execution_plan", return_value=(plan, ())), patch.object(
            document_coordinator, "_has_inventory_fingerprint_field", return_value=True
        ), patch.object(document_coordinator, "_find_by_fingerprint", return_value=None), patch.object(
            document_coordinator, "_find_open_material_request_intent", return_value=None
        ), patch.object(
            document_coordinator.frappe, "get_meta", return_value=SimpleNamespace(has_field=lambda _field: True)
        ), patch.object(
            document_coordinator.frappe.db,
            "get_value",
            side_effect=lambda _doctype, _name, field, **_kwargs: (
                {"company": "Cafe Co", "is_group": 0, "disabled": 0}
                if isinstance(field, list)
                else "Cafe Co"
            ),
        ), patch.object(
            document_coordinator, "upsert_inventory_exception", return_value="EX-1"
        ) as upsert:
            result = document_coordinator.create_and_submit_material_request(
                company="Cafe Co",
                purpose="Transfer",
                required_date="2026-08-16",
                lines=(line,),
                plan_hash="plan-hash",
                policy_hash="policy-hash",
                transit_warehouse="Transit",
            )
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["exception"], "EX-1")
        self.assertEqual(upsert.call_args.kwargs["warehouse"], "Destination")

    def test_dispatch_and_receipt_use_the_approved_two_stage_route(self) -> None:
        material_request = SimpleNamespace(
            name="MAT-TRANSFER-1",
            modified="2026-08-15 10:00:00",
            company="Cafe Co",
            docstatus=1,
            material_request_type="Material Transfer",
            custom_kopos_transit_warehouse="Transit",
            items=[SimpleNamespace(
                name="MAT-ITEM-1",
                item_code="MILK",
                from_warehouse="Source",
                warehouse="Destination",
                qty=Decimal("10"),
                uom="Pack",
                stock_uom="Each",
                conversion_factor=Decimal("12"),
                stock_qty=Decimal("120"),
            )],
        )
        common = {
            "company": "Cafe Co",
            "material_request": "MAT-TRANSFER-1",
            "source_document": "MAT-TRANSFER-1",
            "source_revision": "2026-08-15T10:00:00+08:00",
            "lines": [{
                "item_code": "MILK",
                "material_request_item": "MAT-ITEM-1",
            "qty": "10",
            "uom": "Pack",
            "stock_uom": "Each",
            "conversion_factor": "12",
                "batch_no": "BATCH-1",
            }],
        }

        def exists(doctype: str, _name: object = None, **_kwargs: object) -> bool:
            return doctype in {"Material Request", "DocType", "Stock Entry", "Stock Entry Detail"}

        def run(dispatch: bool, warehouse: str, progress: tuple[Decimal, Decimal], entry_name: str) -> _StockEntry:
            value = {**common, "warehouse": warehouse}
            entry = _StockEntry(entry_name)
            with patch.object(inventory_api, "frappe") as fake_frappe, patch.object(
                inventory_api, "_transfer_route_tracking_installed", return_value=True
            ), patch.object(
                inventory_api, "_material_request_stock_quantity", return_value=Decimal("10")
            ), patch.object(
                inventory_api, "_transfer_route_progress", return_value=progress
            ), patch.object(inventory_api, "_require_transfer_batch_metadata"), patch.object(
                inventory_api, "_apply_guided_task_audit"
            ), patch.object(inventory_api, "_set_document_value"):
                fake_frappe.db.exists.side_effect = exists
                fake_frappe.db.get_value.return_value = "Cafe Co"
                fake_frappe.get_meta.return_value = SimpleNamespace(has_field=lambda _field: True)
                fake_frappe.get_doc.return_value = material_request
                fake_frappe.new_doc.return_value = entry
                fake_frappe.ValidationError = frappe.ValidationError
                fake_frappe.PermissionError = frappe.PermissionError
                fake_frappe._ = lambda value: value
                fake_frappe.throw.side_effect = lambda message, exc=None: (_ for _ in ()).throw((exc or frappe.ValidationError)(message))
                # The API's module-level ``_`` and ``frappe`` are both patched
                # so this exercises the route validation without a live site.
                result = inventory_api._create_transfer_entry(value, f"CMD-{entry_name}", dispatch=dispatch)
            self.assertEqual(result, entry_name)
            return entry

        dispatch_entry = run(True, "Source", (Decimal("0"), Decimal("0")), "SE-DISPATCH")
        self.assertEqual(dispatch_entry.items[0]["s_warehouse"], "Source")
        self.assertEqual(dispatch_entry.items[0]["t_warehouse"], "Transit")
        self.assertEqual(dispatch_entry.items[0]["uom"], "Pack")
        self.assertEqual(dispatch_entry.items[0]["stock_uom"], "Each")
        self.assertEqual(dispatch_entry.items[0]["conversion_factor"], Decimal("12"))
        self.assertEqual(dispatch_entry.items[0]["transfer_qty"], Decimal("120"))

        receipt_entry = run(False, "Destination", (Decimal("120"), Decimal("0")), "SE-RECEIPT")
        self.assertEqual(receipt_entry.items[0]["s_warehouse"], "Transit")
        self.assertEqual(receipt_entry.items[0]["t_warehouse"], "Destination")


if __name__ == "__main__":
    import unittest

    unittest.main()
