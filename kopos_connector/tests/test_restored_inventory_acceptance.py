from __future__ import annotations

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
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
    def test_expense_account_uses_company_default_and_validates_it(self) -> None:
        class _AccountFrappe(_FakeFrappe):
            def get_all(self, doctype: str, *args: object, **kwargs: object) -> list[dict[str, str]]:
                if doctype == "Company":
                    return [{"default_expense_account": "COGS - CERC"}]
                if doctype == "Account":
                    return [{"name": "COGS - CERC"}]
                return []

        with patch.object(acceptance, "frappe", _AccountFrappe([])):
            self.assertEqual(
                acceptance._discover_expense_account("CERC"),
                "COGS - CERC",
            )

        class _MissingDefaultFrappe(_FakeFrappe):
            def get_all(self, doctype: str, *args: object, **kwargs: object) -> list[dict[str, str]]:
                if doctype == "Company":
                    return [{"default_expense_account": ""}]
                return [{"name": "Alphabetical Expense - CERC"}]

        with patch.object(acceptance, "frappe", _MissingDefaultFrappe([])):
            with self.assertRaisesRegex(ValueError, "default expense/COGS account is required"):
                acceptance._discover_expense_account("CERC")

        class _InvalidDefaultFrappe(_FakeFrappe):
            def get_all(self, doctype: str, *args: object, **kwargs: object) -> list[dict[str, str]]:
                if doctype == "Company":
                    return [{"default_expense_account": "Not Expense - CERC"}]
                return []

        with patch.object(acceptance, "frappe", _InvalidDefaultFrappe([])):
            with self.assertRaisesRegex(ValueError, "missing, disabled, grouped"):
                acceptance._discover_expense_account("CERC")

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

    def test_opening_reconciliation_uses_difference_account_field_authority(self) -> None:
        class _Document:
            doctype = "Stock Reconciliation"
            docstatus = 0

            def __init__(self) -> None:
                self.appended: list[tuple[str, object]] = []

            def append(self, fieldname: str, value: object) -> None:
                self.appended.append((fieldname, value))

            def insert(self, **_kwargs: object) -> None:
                return None

            def submit(self) -> None:
                self.docstatus = 1

        fake = _FakeFrappe([])
        fake.db = SimpleNamespace(exists=lambda *_args: None)
        fake.new_doc = lambda _doctype: _Document()
        fake.utils = SimpleNamespace(
            cstr=lambda value: "" if value is None else str(value),
            nowdate=lambda: "2026-08-16",
            now_datetime=lambda: datetime(2026, 8, 16, 12, 0),
        )

        with patch.object(acceptance, "frappe", fake), patch.object(
            acceptance, "_set_if_present"
        ) as set_if_present:
            reconciliation = acceptance._ensure_opening_reconciliation(
                company="CERC",
                warehouse="Main - CERC",
                ingredient_item="INV-ACCEPT-INGREDIENT",
                uom="Nos",
                difference_account="Temporary Opening - CERC",
            )

        self.assertEqual(reconciliation.docstatus, 1)
        self.assertEqual(reconciliation.purpose, "Opening Stock")
        self.assertEqual(reconciliation.appended[0][1]["stock_uom"], "Nos")
        assignments = {
            call.args[1]: call.args[2] for call in set_if_present.call_args_list
        }
        self.assertEqual(assignments["expense_account"], "Temporary Opening - CERC")
        self.assertNotIn("difference_account", assignments)

    def test_policy_creation_uses_real_insert_for_new_named_fixture(self) -> None:
        class _Policy:
            doctype = "FB Inventory Policy"

            def __init__(self) -> None:
                self.inserted = False
                self.saved = False
                self.name = "New FB Inventory Policy"
                self.insert_kwargs: dict[str, object] = {}

            def is_new(self) -> bool:
                return not self.inserted

            def insert(self, **kwargs: object) -> None:
                self.inserted = True
                self.insert_kwargs = kwargs

            def save(self, **_kwargs: object) -> None:
                self.saved = True

        policy = _Policy()
        fake = _FakeFrappe([])
        fake.db = SimpleNamespace(exists=lambda *_args: None)
        fake.new_doc = lambda _doctype: policy
        fake.utils = SimpleNamespace(
            now_datetime=lambda: datetime(2026, 8, 16, 12, 0),
        )

        with patch.object(acceptance, "frappe", fake):
            result = acceptance._ensure_policy(
                company="CERC",
                warehouse="Main - CERC",
                opening_name="INV-ACCEPT-OPENING",
                expense_account="COGS - CERC",
            )

        self.assertIs(result, policy)
        self.assertEqual(policy.name, acceptance.POLICY_NAME)
        self.assertTrue(policy.inserted)
        self.assertEqual(policy.insert_kwargs.get("set_name"), acceptance.POLICY_NAME)
        self.assertFalse(policy.saved)

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
