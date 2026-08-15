from __future__ import annotations

import json
from pathlib import Path
import unittest
from unittest.mock import patch

from kopos_connector.acceptance.restored_outlet_matrix import (
    CONTRACT_ID,
    EVIDENCE_LEVEL,
    REQUIRED_CATEGORIES,
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
