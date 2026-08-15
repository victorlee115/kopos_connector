from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from contextlib import nullcontext
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from kopos_connector.tests.fake_frappe import install_fake_frappe_modules


install_fake_frappe_modules()

import frappe

from kopos_connector.kopos.services.inventory_autopilot import document_coordinator
from kopos_connector.kopos.services.inventory_autopilot.document_coordinator import (
    _find_open_material_request_intent,
    _plan_line_uoms,
    _plan_quantity,
    _validate_material_request_quotation_authority,
    create_and_submit_material_request,
)
from kopos_connector.kopos.services.inventory_autopilot.replenishment import ReplenishmentLine
from kopos_connector.kopos.services.inventory_autopilot.recipe_compiler import (
    compile_recipe_components,
)


ERP_ROOT = Path(__file__).resolve().parents[1]


class InventoryDecimalAuthorityTests(unittest.TestCase):
    def test_supplier_quotation_requires_one_exact_reference_per_request_row(self):
        request = SimpleNamespace(
            name="MAT-ROW-AUTHORITY",
            items=[
                SimpleNamespace(
                    name="MRI-1",
                    item_code="MILK",
                    warehouse="Outlet",
                    uom="Litre",
                    qty=Decimal("1"),
                    conversion_factor=Decimal("1"),
                ),
                SimpleNamespace(
                    name="MRI-2",
                    item_code="MILK",
                    warehouse="Outlet",
                    uom="Litre",
                    qty=Decimal("1"),
                    conversion_factor=Decimal("1"),
                ),
            ],
        )

        def quotation_row(request_item: str) -> SimpleNamespace:
            return SimpleNamespace(
                material_request=request.name,
                material_request_item=request_item,
                item_code="MILK",
                warehouse="Outlet",
                uom="Litre",
                qty=Decimal("1"),
                conversion_factor=Decimal("1"),
            )

        quotation = SimpleNamespace(items=[quotation_row("MRI-1"), quotation_row("MRI-2")])
        self.assertIsNone(_validate_material_request_quotation_authority(request, quotation))

        quotation.items[1].material_request_item = "MRI-1"
        self.assertEqual(
            _validate_material_request_quotation_authority(request, quotation),
            "supplier_quotation_does_not_exactly_match_material_request",
        )

    def test_authoritative_doctypes_have_hidden_decimal_text_fields(self):
        expected = {
            "fb_recipe": {"yield_qty_decimal", "default_serving_qty_decimal"},
            "fb_recipe_component": {
                "qty_decimal",
                "stock_qty_decimal",
                "stock_conversion_factor_decimal",
                "loss_factor_pct_decimal",
            },
            "fb_modifier": {"qty_delta_decimal", "scale_percent_decimal"},
            "fb_recipe_modifier_effect": {
                "qty_delta_decimal",
                "stock_qty_delta_decimal",
                "stock_conversion_factor_decimal",
                "scale_percent_decimal",
            },
            "fb_inventory_plan_line": {"quantity_decimal"},
        }
        for doctype, fieldnames in expected.items():
            payload = json.loads(
                (ERP_ROOT / "kopos" / "doctype" / doctype / f"{doctype}.json").read_text()
            )
            fields = {field["fieldname"]: field for field in payload["fields"]}
            for fieldname in fieldnames:
                self.assertIn(fieldname, fields)
                self.assertEqual(fields[fieldname]["fieldtype"], "Data")
                self.assertEqual(fields[fieldname].get("hidden"), 1)

    def test_recipe_compiler_prefers_exact_text_for_high_precision_execution(self):
        result = compile_recipe_components(
            {
                "yield_qty": 1.0,
                "yield_qty_decimal": "3.0000000000000001",
                "default_serving_qty": 1.0,
                "default_serving_qty_decimal": "1.0000000000000001",
                "components": [
                    {
                        "item": "SYRUP",
                        "stock_qty": 0.0,
                        "stock_qty_decimal": "0.3703703670370370367",
                        "stock_uom": "L",
                    }
                ],
            },
            servings="1.0000000000000001",
        )
        self.assertEqual(
            result["SYRUP"],
            Decimal("0.3703703670370370367") / Decimal("3.0000000000000001"),
        )

    def test_plan_execution_reads_exact_quantity_before_float_display(self):
        exact = "0.1234567890123456789"
        self.assertEqual(
            _plan_quantity({"quantity": 0.123457, "quantity_decimal": exact}),
            Decimal(exact),
        )
        self.assertEqual(_plan_quantity({"quantity": 2.5}), Decimal("2.5"))

    def test_plan_uom_key_retains_all_significant_decimal_places(self):
        quantity = Decimal("0.1234567890123456789")
        line = ReplenishmentLine("MILK", "Outlet", quantity, "shortfall")
        plan = {
            "lines": [{
                "item": "MILK",
                "action": "Purchase",
                "warehouse": "Outlet",
                "source_warehouse": "",
                "quantity": "0.123457",
                "quantity_decimal": str(quantity),
                "uom": "Litre",
            }],
        }
        with patch.object(
            frappe.db,
            "get_value",
            return_value="Litre",
        ):
            uoms = _plan_line_uoms(plan, purpose="Purchase", lines=(line,))
        self.assertEqual(uoms[("MILK", "Outlet", "", quantity)], "Litre")

    def test_open_intent_does_not_collapse_distinct_high_precision_quantities(self):
        expected_quantity = Decimal("0.1234567890123456789")
        document = SimpleNamespace(
            name="MAT-EXACT",
            items=[SimpleNamespace(
                item_code="MILK",
                warehouse="Outlet",
                from_warehouse=None,
                qty=Decimal("0.1234567890123456788"),
            )],
        )
        with patch.object(frappe.db, "exists", return_value=True), patch.object(
            frappe,
            "get_all",
            return_value=[{"name": "MAT-EXACT", "schedule_date": "2026-08-16", "status": "Open"}],
        ), patch.object(frappe, "get_doc", return_value=document):
            self.assertIsNone(_find_open_material_request_intent(
                company="Cafe Co",
                purpose="Purchase",
                required_date="2026-08-16",
                rows=(ReplenishmentLine("MILK", "Outlet", expected_quantity, "shortfall"),),
            ))
            document.items[0].qty = expected_quantity
            self.assertEqual(
                _find_open_material_request_intent(
                    company="Cafe Co",
                    purpose="Purchase",
                    required_date="2026-08-16",
                    rows=(ReplenishmentLine("MILK", "Outlet", expected_quantity, "shortfall"),),
                ),
                "MAT-EXACT",
            )

    def test_duplicate_insert_race_returns_existing_high_precision_request(self):
        quantity = Decimal("0.1234567890123456789")

        class DuplicateEntryError(Exception):
            pass

        class Request:
            doctype = "Material Request"
            name = "MAT-RACE"
            docstatus = 0

            def __init__(self):
                self.items = []

            def append(self, fieldname, value):
                self.items.append(value)

            def insert(self):
                raise DuplicateEntryError("unique inventory fingerprint")

        plan = {
            "name": "PLAN-EXACT",
            "warehouse": "Outlet",
            "gate_results": json.dumps({name: True for name in (
                "policy_active", "automation_identity", "input_hash_match",
                "no_unresolved_count", "devices_current", "projection_backlog_clear",
                "recipe_uom_complete", "forecast_reliable", "source_current",
                "quantity_ceiling", "value_ceiling", "shelf_life_cap", "intent_not_open",
            )}),
            "lines": [{
                "item": "MILK",
                "action": "Purchase",
                "warehouse": "Outlet",
                "source_warehouse": "",
                "quantity": "0.123457",
                "quantity_decimal": str(quantity),
                "uom": "Litre",
            }],
        }
        line = ReplenishmentLine("MILK", "Outlet", quantity, "shortfall")
        duplicate_error = getattr(frappe, "DuplicateEntryError", None)
        setattr(frappe, "DuplicateEntryError", DuplicateEntryError)
        try:
            with patch.object(
                document_coordinator,
                "_execution_plan",
                return_value=(plan, ()),
            ), patch.object(document_coordinator, "_has_inventory_fingerprint_field", return_value=True), patch.object(
                document_coordinator,
                "_find_by_fingerprint",
                side_effect=[None, "MAT-RACE"],
            ), patch.object(
                document_coordinator,
                "_find_open_material_request_intent",
                return_value=None,
            ), patch.object(
                document_coordinator,
                "_plan_line_uoms",
                return_value={("MILK", "Outlet", "", quantity): "Litre"},
            ), patch.object(document_coordinator, "_record_plan_document"), patch.object(
                document_coordinator.frappe,
                "new_doc",
                return_value=Request(),
            ), patch.object(
                document_coordinator.frappe,
                "get_meta",
                return_value=SimpleNamespace(has_field=lambda _field: True),
            ), patch.object(
                document_coordinator,
                "inventory_automation_identity",
                return_value=nullcontext("AUTO"),
            ):
                result = create_and_submit_material_request(
                    company="Cafe Co",
                    purpose="Purchase",
                    required_date="2026-08-16",
                    lines=(line,),
                    plan_hash="plan-hash",
                    policy_hash="policy-hash",
                )
        finally:
            if duplicate_error is None:
                delattr(frappe, "DuplicateEntryError")
            else:
                setattr(frappe, "DuplicateEntryError", duplicate_error)
        self.assertEqual(result["status"], "duplicate")
        self.assertEqual(result["material_request"], "MAT-RACE")


if __name__ == "__main__":
    unittest.main()
