from __future__ import annotations

from pathlib import Path
import unittest
from unittest.mock import patch

import kopos_connector.acceptance.restored_inventory_acceptance as acceptance
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


class _FakeFrappe:
    class ValidationError(ValueError):
        pass

    class _Utils:
        @staticmethod
        def cstr(value: object) -> str:
            return "" if value is None else str(value)

    utils = _Utils()

    def __init__(self, rows: list[dict[str, str]]) -> None:
        self.rows = rows

    def get_all(self, *_args: object, **_kwargs: object) -> list[dict[str, str]]:
        return self.rows

    def throw(self, message: str, exc: type[Exception] | None = None) -> None:
        raise (exc or self.ValidationError)(message)


class TestRestoredInventoryAcceptance(unittest.TestCase):
    def test_opening_difference_account_uses_one_existing_semantic_authority(self) -> None:
        fake = _FakeFrappe(
            [
                {"name": "Stock In Hand - CERC", "account_type": "Stock"},
                {"name": "Temporary Opening - CERC", "account_type": "Temporary"},
            ]
        )
        with patch.object(acceptance, "frappe", fake):
            self.assertEqual(
                acceptance._discover_opening_difference_account("CERC"),
                "Temporary Opening - CERC",
            )

        ambiguous = _FakeFrappe(
            [
                {"name": "Temporary A - CERC", "account_type": "Temporary"},
                {"name": "Temporary B - CERC", "account_type": "Temporary"},
            ]
        )
        with patch.object(acceptance, "frappe", ambiguous):
            with self.assertRaisesRegex(ValueError, "Difference Account is ambiguous"):
                acceptance._discover_opening_difference_account("CERC")

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
