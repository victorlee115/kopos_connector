from __future__ import annotations

import json
from pathlib import Path
import unittest
from unittest.mock import patch

from kopos_connector.acceptance.restored_outlet_matrix import (
    CONTRACT_ID,
    EVIDENCE_LEVEL,
    REQUIRED_CATEGORIES,
    _items,
    _suppliers_and_quotations,
    _uoms,
    run_v1,
    validate_outlet_matrix_report,
)


def _report(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "schemaVersion": 1,
        "contractId": CONTRACT_ID,
        "status": "passed",
        "evidenceLevel": EVIDENCE_LEVEL,
        "producer": "kopos_connector.acceptance.restored_outlet_matrix.run_v1",
        "readOnly": True,
        "providerNetworkCalls": 0,
        "connectorVersion": "1.0.11",
        "erpArtifactSha256": "a" * 64,
        "restoredBackupSha256": "b" * 64,
        "scope": "restored_site",
        "fixtureExclusion": {
            "prefixes": ["INV-ACCEPT-", "SMOKE-"],
            "classification": "explicit_prefix_and_marker_only",
            "excludedCounts": {},
            "excludedIdHashesByDoctype": {},
        },
        "missingAuthorities": [],
        "sourceCounts": {},
        "permissionAudit": {
            "status": "passed",
            "outletUserRoleViolations": 0,
            "systemManagerHandling": "technical_admin_explicitly_reported",
            "companyDirectorHandling": "business_authorized",
        },
    }
    for category_name in REQUIRED_CATEGORIES:
        value[category_name] = {
            "realCount": 0,
            "fixtureCount": 0,
            "configured": False,
            "ready": False,
            "missingReasons": [],
        }
    value.update(overrides)
    return value


class TestRestoredOutletMatrix(unittest.TestCase):
    def test_contract_accepts_a_read_only_bound_report(self) -> None:
        report = validate_outlet_matrix_report(_report())
        self.assertEqual(report["contractId"], CONTRACT_ID)

    def test_contract_rejects_writes_or_unbound_hashes(self) -> None:
        for overrides, expected in (
            ({"readOnly": False}, "readOnly"),
            ({"providerNetworkCalls": 1}, "providerNetworkCalls"),
            ({"erpArtifactSha256": "not-a-hash"}, "erpArtifactSha256"),
        ):
            with self.subTest(overrides=overrides):
                with self.assertRaisesRegex(ValueError, expected):
                    validate_outlet_matrix_report(_report(**overrides))

    def test_contract_requires_every_preflight_category(self) -> None:
        value = _report()
        del value["companies"]
        with self.assertRaisesRegex(ValueError, "companies"):
            validate_outlet_matrix_report(value)

    def test_contract_requires_permission_audit_binding(self) -> None:
        value = _report()
        del value["permissionAudit"]
        with self.assertRaisesRegex(ValueError, "permissionAudit"):
            validate_outlet_matrix_report(value)

    def test_source_has_no_mutating_frappe_operations(self) -> None:
        source = Path(__file__).parents[1] / "acceptance" / "restored_outlet_matrix.py"
        text = source.read_text(encoding="utf-8")
        for forbidden in (".insert(", ".save(", "db.set_value", "db.commit", "enqueue"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, text)

    def test_items_report_standard_replenishment_authorities(self) -> None:
        from kopos_connector.acceptance import restored_outlet_matrix as matrix

        ctx = {"fixtureCounts": {}}
        item_rows = [
            {
                "name": "ING-MILK",
                "is_stock_item": 1,
                "is_sales_item": 0,
                "is_purchase_item": 1,
                "disabled": 0,
                "item_group": "Ingredients",
                "stock_uom": "Litre",
                "purchase_uom": "Carton",
                "min_order_qty": "2",
                "lead_time_days": "3",
                "custom_kopos_inventory_classification": "Purchased stock Item",
                # These fields deliberately model legacy/ad-hoc data.  The
                # matrix must not request or report them as authority.
                "custom_kopos_supplier_pack_size": "99",
                "custom_kopos_supplier_minimum_qty": "99",
            }
        ]
        with patch.object(matrix, "_rows", return_value=item_rows) as read_rows:
            category, returned_rows = _items(ctx)

        self.assertEqual(returned_rows, item_rows)
        self.assertEqual(category["purchaseUomConfiguredCount"], 1)
        self.assertEqual(category["minimumOrderQuantityConfiguredCount"], 1)
        self.assertEqual(category["leadTimeConfiguredCount"], 1)
        self.assertNotIn("supplierPackConfiguredCount", category)
        self.assertNotIn("supplierMinimumConfiguredCount", category)
        requested_fields = read_rows.call_args.args[2]
        self.assertIn("purchase_uom", requested_fields)
        self.assertIn("min_order_qty", requested_fields)
        self.assertIn("lead_time_days", requested_fields)
        self.assertNotIn("custom_kopos_supplier_pack_size", requested_fields)
        self.assertNotIn("custom_kopos_supplier_minimum_qty", requested_fields)

    def test_item_supplier_rows_are_only_an_allow_list(self) -> None:
        from kopos_connector.acceptance import restored_outlet_matrix as matrix

        ctx = {"fixtureCounts": {}}
        rows_by_doctype = {
            "Supplier": [{"name": "SUP-MILK", "supplier_group": "Local"}],
            "Item Supplier": [
                {
                    "parent": "ING-MILK",
                    "supplier": "SUP-MILK",
                    "supplier_part_no": "MILK-01",
                    # Legacy/ad-hoc values must not become authority.
                    "lead_time_days": "99",
                    "custom_kopos_lead_time_days": "99",
                }
            ],
            "Supplier Quotation": [],
            "Supplier Quotation Item": [],
        }

        def read_rows(_ctx, doctype, fields, _category, **_kwargs):
            if doctype == "Item Supplier":
                self.assertEqual(
                    fields,
                    ("parent", "supplier", "supplier_part_no"),
                )
            return rows_by_doctype[doctype]

        with patch.object(matrix, "_rows", side_effect=read_rows):
            supplier_category, _quotation_category = _suppliers_and_quotations(ctx)

        self.assertEqual(supplier_category["itemSupplierAllowListCount"], 1)
        self.assertNotIn("supplierLeadTimeConfiguredCount", supplier_category)
        self.assertNotIn("supplierPackConfiguredCount", supplier_category)
        self.assertNotIn("supplierMinimumConfiguredCount", supplier_category)

    def test_uom_report_reads_standard_item_conversion_detail(self) -> None:
        from kopos_connector.acceptance import restored_outlet_matrix as matrix

        ctx = {"fixtureCounts": {}}
        rows_by_doctype = {
            "UOM": [{"name": "Litre", "enabled": 1}],
            "UOM Conversion Detail": [
                {
                    "parent": "ING-MILK",
                    "uom": "Carton",
                    "conversion_factor": "12",
                }
            ],
            "FB Recipe Component": [],
        }

        def read_rows(_ctx, doctype, fields, _category, **_kwargs):
            if doctype == "UOM Conversion Detail":
                self.assertEqual(
                    fields,
                    ("parent", "uom", "conversion_factor"),
                )
            return rows_by_doctype[doctype]

        with patch.object(matrix, "_rows", side_effect=read_rows):
            category = _uoms(ctx)

        self.assertEqual(category["itemUomConversionRowCount"], 1)
        self.assertEqual(category["uomCount"], 1)

    def test_fixture_prefix_is_explicit_in_contract(self) -> None:
        fixture = _report()["fixtureExclusion"]
        assert isinstance(fixture, dict)
        self.assertIn("INV-ACCEPT-", fixture["prefixes"])
        self.assertEqual(
            fixture["classification"], "explicit_prefix_and_marker_only"
        )

    def test_runtime_report_is_sanitized_and_records_missing_authorities(self) -> None:
        class Meta:
            def __init__(self, fields: set[str]) -> None:
                self.fields = fields

            def has_field(self, fieldname: str) -> bool:
                return fieldname in self.fields

        class Database:
            def get_single_value(self, _doctype: str, _fieldname: str) -> str:
                return "Asia/Kuala_Lumpur"

        class FakeFrappe:
            db = Database()
            conf: dict[str, str] = {}
            rows = {
                "Company": [{"name": "Cafe Co", "default_currency": "MYR", "abbr": "CC"}],
                "Warehouse": [{"name": "Main - CC", "company": "Cafe Co", "is_group": 0}],
                "POS Profile": [{"name": "Cafe POS", "company": "Cafe Co", "warehouse": "Main - CC"}],
                "FB Inventory Policy": [{"name": "POL", "company": "Cafe Co", "warehouse": "Main - CC"}],
                "Item": [{"name": "Coffee", "is_sales_item": 1, "is_stock_item": 1, "disabled": 0}],
                "UOM": [{"name": "Nos"}],
                "FB Order": [{"name": "ORD", "sale_datetime": "2026-08-10T10:00:00+08:00"}],
            }

            def get_meta(self, doctype: str) -> Meta:
                fields = {"name"}
                for row in self.rows.get(doctype, []):
                    fields.update(row)
                return Meta(fields)

            def get_all(self, doctype: str, **_kwargs: object) -> list[dict[str, object]]:
                return list(self.rows.get(doctype, []))

        from kopos_connector.acceptance import restored_outlet_matrix as matrix

        with patch.object(matrix, "frappe", FakeFrappe()), patch.object(
            matrix.metadata, "version", return_value="1.0.11"
        ):
            report = run_v1("a" * 64, "b" * 64, "1.0.11")

        self.assertEqual(report["readOnly"], True)
        self.assertEqual(report["providerNetworkCalls"], 0)
        self.assertIn("no_employee_authority", json.dumps(report))
        self.assertNotIn("Cafe Co", json.dumps(report))
        self.assertEqual(report["historicalEvidence"]["historicalRecipesRewritten"], False)


if __name__ == "__main__":
    unittest.main()
