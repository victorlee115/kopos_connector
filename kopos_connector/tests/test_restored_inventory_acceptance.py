from __future__ import annotations

from pathlib import Path
import unittest

from kopos_connector.acceptance.restored_inventory_acceptance import (
    CONTRACT_ID,
    validate_inventory_acceptance_proof,
)


def _proof(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "contractId": CONTRACT_ID,
        "status": "passed",
        "resolvedSaleCount": 1,
        "stockEntryCount": 1,
        "stockLedgerEntryCount": 1,
        "glEntryCount": 2,
        "duplicateTargetCount": 0,
    }
    value.update(overrides)
    return value


class TestRestoredInventoryAcceptance(unittest.TestCase):
    def test_inventory_acceptance_contract_requires_positive_real_evidence(self) -> None:
        self.assertEqual(validate_inventory_acceptance_proof(_proof())["glEntryCount"], 2)

        for fieldname in (
            "resolvedSaleCount",
            "stockEntryCount",
            "stockLedgerEntryCount",
            "glEntryCount",
        ):
            with self.assertRaisesRegex(ValueError, fieldname):
                validate_inventory_acceptance_proof(_proof(**{fieldname: 0}))

    def test_inventory_acceptance_contract_rejects_duplicate_target_or_boolean_counts(self) -> None:
        with self.assertRaisesRegex(ValueError, "duplicateTargetCount"):
            validate_inventory_acceptance_proof(_proof(duplicateTargetCount=1))
        with self.assertRaisesRegex(ValueError, "positive integer"):
            validate_inventory_acceptance_proof(_proof(stockEntryCount=True))

    def test_harness_has_a_separate_contained_producer_and_read_only_catalog_binding(self) -> None:
        script = Path(__file__).parents[3] / "JiJiPOS" / "scripts" / "erp-test.sh"
        text = script.read_text(encoding="utf-8")
        self.assertIn("restored-inventory-acceptance) cmd_restored_inventory_acceptance", text)
        self.assertIn("from kopos_connector.acceptance.restored_inventory_acceptance import read_v1", text)
        self.assertIn("sanitize_restored_site >/dev/null", text)
        self.assertIn("ERP_INSTALL_MODE", text)
