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


class _OpeningDocument:
    doctype = "Stock Reconciliation"

    def __init__(
        self,
        runtime: "_OpeningFrappe",
        *,
        name: str,
        docstatus: int = 0,
        company: str = "CERC",
        purpose: str = "Opening Stock",
        remarks: str = acceptance.OPENING_REMARKS,
        items: list[SimpleNamespace] | None = None,
    ) -> None:
        self.runtime = runtime
        self.name = name
        self.docstatus = docstatus
        self.company = company
        self.purpose = purpose
        self.remarks = remarks
        self.items = list(items or [])
        self.saved = False
        self.inserted = False
        self.cancelled = False

    def append(self, fieldname: str, value: object) -> SimpleNamespace:
        if fieldname != "items" or not isinstance(value, dict):
            raise AssertionError("unexpected append")
        row = SimpleNamespace(**value)
        self.items.append(row)
        return row

    def insert(self, **_kwargs: object) -> None:
        self.inserted = True
        self.name = self.runtime.allocate_reconciliation_name()
        self.runtime.documents[self.name] = self

    def save(self, **_kwargs: object) -> None:
        self.saved = True

    def submit(self) -> None:
        self.docstatus = 1
        self.runtime.stock_ledger_rows[self.name] = [{"actual_qty": "10"}]

    def cancel(self) -> None:
        self.docstatus = 2
        self.cancelled = True
        self.runtime.stock_ledger_rows[self.name] = []


class _OpeningFrappe(_FakeFrappe):
    def __init__(self, documents: list[_OpeningDocument] | None = None) -> None:
        super().__init__([])
        self.documents = {document.name: document for document in documents or []}
        self.stock_ledger_rows: dict[str, list[dict[str, str]]] = {}
        self.new_doc_calls = 0
        self._name_sequence = len(self.documents) + 1
        self.db = SimpleNamespace(exists=self._exists)
        self.utils = SimpleNamespace(
            cstr=lambda value: "" if value is None else str(value),
            nowdate=lambda: "2026-08-16",
            now_datetime=lambda: datetime(2026, 8, 16, 12, 0),
        )

    def allocate_reconciliation_name(self) -> str:
        while True:
            name = f"MAT-RECO-2026-{self._name_sequence:05d}"
            self._name_sequence += 1
            if name not in self.documents:
                return name

    def _exists(self, doctype: str, name: str) -> str | None:
        if doctype == "Stock Reconciliation" and name in self.documents:
            return name
        return None

    def get_all(
        self, doctype: str, *_args: object, **kwargs: object
    ) -> list[dict[str, str]]:
        if doctype == "Stock Reconciliation":
            filters = kwargs.get("filters")
            assert isinstance(filters, dict)
            return [
                {"name": name}
                for name, document in self.documents.items()
                if document.company == filters.get("company")
                and document.remarks == filters.get("remarks")
            ]
        if doctype == "Stock Ledger Entry":
            filters = kwargs.get("filters")
            assert isinstance(filters, dict)
            return list(self.stock_ledger_rows.get(str(filters.get("voucher_no")), []))
        if doctype == "Cost Center":
            return []
        return []

    def get_doc(self, doctype: str, name: str) -> _OpeningDocument:
        if doctype != "Stock Reconciliation":
            raise AssertionError(f"unexpected doctype {doctype}")
        return self.documents[name]

    def new_doc(self, doctype: str) -> _OpeningDocument:
        if doctype != "Stock Reconciliation":
            raise AssertionError(f"unexpected doctype {doctype}")
        self.new_doc_calls += 1
        return _OpeningDocument(
            self,
            name=f"New Stock Reconciliation {self.new_doc_calls}",
            company="",
            purpose="",
            remarks="",
        )

    def get_meta(self, _doctype: str) -> SimpleNamespace:
        return SimpleNamespace(has_field=lambda _fieldname: True)


def _opening_item(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "item_code": acceptance.ITEM_INGREDIENT,
        "warehouse": "Main - CERC",
        "qty": "10",
        "stock_uom": "Nos",
        "valuation_rate": "1",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


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
        fake = _OpeningFrappe()

        with patch.object(acceptance, "frappe", fake):
            reconciliation = acceptance._ensure_opening_reconciliation(
                company="CERC",
                warehouse="Main - CERC",
                ingredient_item="INV-ACCEPT-INGREDIENT",
                uom="Nos",
                difference_account="Temporary Opening - CERC",
            )

        self.assertEqual(reconciliation.docstatus, 1)
        self.assertEqual(reconciliation.purpose, "Opening Stock")
        self.assertEqual(reconciliation.items[0].stock_uom, "Nos")
        self.assertEqual(reconciliation.expense_account, "Temporary Opening - CERC")
        self.assertFalse(hasattr(reconciliation, "difference_account"))
        self.assertNotEqual(reconciliation.name, acceptance.OPENING_NAME)

    def test_opening_reconciliation_recovers_autonamed_submitted_fixture(self) -> None:
        fake = _OpeningFrappe()
        existing = _OpeningDocument(
            fake,
            name="MAT-RECO-2026-00001",
            docstatus=1,
            items=[_opening_item()],
        )
        fake.documents[existing.name] = existing
        fake.stock_ledger_rows[existing.name] = [{"actual_qty": "10.000"}]

        with patch.object(acceptance, "frappe", fake):
            first = acceptance._ensure_opening_reconciliation(
                company="CERC",
                warehouse="Main - CERC",
                ingredient_item=acceptance.ITEM_INGREDIENT,
                uom="Nos",
                difference_account="Temporary Opening - CERC",
            )
            second = acceptance._ensure_opening_reconciliation(
                company="CERC",
                warehouse="Main - CERC",
                ingredient_item=acceptance.ITEM_INGREDIENT,
                uom="Nos",
                difference_account="Temporary Opening - CERC",
            )

        self.assertIs(first, existing)
        self.assertIs(second, existing)
        self.assertEqual(fake.new_doc_calls, 0)
        self.assertFalse(existing.cancelled)

    def test_opening_reconciliation_repairs_incomplete_draft(self) -> None:
        fake = _OpeningFrappe()
        draft = _OpeningDocument(
            fake,
            name="MAT-RECO-2026-00001",
            docstatus=0,
            items=[],
        )
        fake.documents[draft.name] = draft

        with patch.object(acceptance, "frappe", fake):
            result = acceptance._ensure_opening_reconciliation(
                company="CERC",
                warehouse="Main - CERC",
                ingredient_item=acceptance.ITEM_INGREDIENT,
                uom="Nos",
                difference_account="Temporary Opening - CERC",
            )

        self.assertIs(result, draft)
        self.assertTrue(draft.saved)
        self.assertEqual(draft.docstatus, 1)
        self.assertEqual(draft.items[0].item_code, acceptance.ITEM_INGREDIENT)
        self.assertEqual(fake.new_doc_calls, 0)

    def test_opening_reconciliation_replaces_exact_zero_movement_fixture(self) -> None:
        fake = _OpeningFrappe()
        partial = _OpeningDocument(
            fake,
            name="MAT-RECO-2026-00001",
            docstatus=1,
            items=[_opening_item()],
        )
        fake.documents[partial.name] = partial
        fake.stock_ledger_rows[partial.name] = [{"actual_qty": "0"}]

        with patch.object(acceptance, "frappe", fake):
            replacement = acceptance._ensure_opening_reconciliation(
                company="CERC",
                warehouse="Main - CERC",
                ingredient_item=acceptance.ITEM_INGREDIENT,
                uom="Nos",
                difference_account="Temporary Opening - CERC",
            )
            replay = acceptance._ensure_opening_reconciliation(
                company="CERC",
                warehouse="Main - CERC",
                ingredient_item=acceptance.ITEM_INGREDIENT,
                uom="Nos",
                difference_account="Temporary Opening - CERC",
            )

        self.assertTrue(partial.cancelled)
        self.assertEqual(partial.docstatus, 2)
        self.assertNotEqual(replacement.name, partial.name)
        self.assertIs(replay, replacement)
        self.assertEqual(fake.new_doc_calls, 1)

    def test_opening_reconciliation_refuses_non_fixture_name_collision(self) -> None:
        fake = _OpeningFrappe()
        collision = _OpeningDocument(
            fake,
            name=acceptance.OPENING_NAME,
            docstatus=1,
            remarks="Real opening stock",
            items=[_opening_item()],
        )
        fake.documents[collision.name] = collision

        with patch.object(acceptance, "frappe", fake):
            with self.assertRaisesRegex(ValueError, "not the acceptance fixture"):
                acceptance._ensure_opening_reconciliation(
                    company="CERC",
                    warehouse="Main - CERC",
                    ingredient_item=acceptance.ITEM_INGREDIENT,
                    uom="Nos",
                    difference_account="Temporary Opening - CERC",
                )

        self.assertFalse(collision.cancelled)
        self.assertEqual(fake.new_doc_calls, 0)

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

    def test_policy_recovery_preserves_cutover_identity_when_opening_is_replaced(self) -> None:
        previous_token = "a" * 64

        class _Policy:
            doctype = "FB Inventory Policy"
            name = acceptance.POLICY_NAME
            company = "CERC"
            warehouse = "Main - CERC"
            cutover_token = previous_token
            cutover_at = datetime(2026, 8, 16, 11, 0)
            inventory_contract_version = "inventory-autopilot-v1"
            permitted_actions = "stock_projection"
            automation_state = "Active"

            def __init__(self) -> None:
                self.opening_stock_reconciliation = "MAT-RECO-2026-00001"
                self.saved = False

            def is_new(self) -> bool:
                return False

            def save(self, **_kwargs: object) -> None:
                self.saved = True

        policy = _Policy()

        class _PolicyFrappe(_FakeFrappe):
            def __init__(self) -> None:
                super().__init__([])
                self.db = SimpleNamespace(
                    exists=lambda doctype, name: (
                        name
                        if doctype == "FB Inventory Policy"
                        and name == acceptance.POLICY_NAME
                        else None
                    )
                )
                self.utils = SimpleNamespace(
                    cstr=lambda value: "" if value is None else str(value),
                    now_datetime=lambda: datetime(2026, 8, 16, 12, 0),
                )

            def get_all(
                self, doctype: str, *_args: object, **_kwargs: object
            ) -> list[dict[str, str]]:
                if doctype != "FB Inventory Policy":
                    return []
                return [
                    {
                        "name": acceptance.POLICY_NAME,
                        "cutover_token": previous_token,
                        "opening_stock_reconciliation": "MAT-RECO-2026-00001",
                    }
                ]

            def get_doc(self, doctype: str, name: str) -> _Policy:
                if doctype != "FB Inventory Policy" or name != acceptance.POLICY_NAME:
                    raise AssertionError("unexpected policy lookup")
                return policy

        with patch.object(acceptance, "frappe", _PolicyFrappe()), patch.object(
            acceptance, "_validate_policy_opening_reference"
        ) as validate_opening:
            result = acceptance._ensure_policy(
                company="CERC",
                warehouse="Main - CERC",
                opening_name="MAT-RECO-2026-00002",
                expense_account="COGS - CERC",
            )

        self.assertIs(result, policy)
        self.assertEqual(policy.cutover_token, previous_token)
        self.assertEqual(
            policy.opening_stock_reconciliation, "MAT-RECO-2026-00002"
        )
        self.assertTrue(policy.saved)
        validate_opening.assert_called_once_with(
            "MAT-RECO-2026-00001", company="CERC", warehouse="Main - CERC"
        )

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
