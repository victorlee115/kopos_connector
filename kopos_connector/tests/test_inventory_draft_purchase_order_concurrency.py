"""Concurrent planning runs must create at most one Draft Purchase Order."""

from __future__ import annotations

from contextlib import nullcontext
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from kopos_connector.tests.fake_frappe import install_fake_frappe_modules


install_fake_frappe_modules()

import frappe

from kopos_connector.kopos.services.inventory_autopilot import document_coordinator


PLAN = {"name": "PLAN-1", "warehouse": "Outlet - JiJi", "material_request": "MAT-1"}


class _PurchaseOrder:
    """Minimal Purchase Order that can be told to lose the insert race."""

    def __init__(self, name: str, *, duplicate_on_insert: bool = False) -> None:
        self.doctype = "Purchase Order"
        self.name = name
        self.docstatus = 0
        self.items: list[dict[str, object]] = []
        self.submitted = False
        self.duplicate_on_insert = duplicate_on_insert
        self.insert_calls = 0

    def append(self, fieldname: str, value: dict[str, object]) -> None:
        assert fieldname == "items"
        self.items.append(value)

    def insert(self, **_kwargs: object) -> None:
        self.insert_calls += 1
        if self.duplicate_on_insert:
            raise frappe.DuplicateEntryError("Purchase Order fingerprint already exists")

    def submit(self) -> None:  # pragma: no cover - must never run
        self.submitted = True
        raise AssertionError("automation must never submit a Purchase Order")


def _quotation() -> SimpleNamespace:
    return SimpleNamespace(
        doctype="Supplier Quotation",
        name="SQ-1",
        docstatus=1,
        supplier="Dairy Supplier",
        currency="MYR",
        transaction_date="2026-08-15",
        valid_till=None,
        items=[SimpleNamespace(
            name="SQ-ITEM-1",
            item_code="MILK",
            qty=10,
            rate=25,
            uom="Pack",
            warehouse="Outlet - JiJi",
        )],
    )


def _material_request() -> SimpleNamespace:
    return SimpleNamespace(
        doctype="Material Request",
        name="MAT-1",
        docstatus=1,
        material_request_type="Purchase",
        schedule_date="2026-08-20",
    )


class DraftPurchaseOrderConcurrencyTests(TestCase):
    def _run(self, *, existing_fingerprint, document):
        created: list[dict[str, str]] = []

        def get_doc(doctype: str, name: str):
            return _quotation() if doctype == "Supplier Quotation" else _material_request()

        with patch.object(document_coordinator, "frappe") as fake_frappe, patch.object(
            document_coordinator, "outbound_configuration_safe", return_value=(True, "")
        ), patch.object(
            document_coordinator, "_execution_plan", return_value=(PLAN, ())
        ), patch.object(
            document_coordinator, "_has_inventory_fingerprint_field", return_value=True
        ), patch.object(
            document_coordinator, "_find_by_fingerprint", side_effect=existing_fingerprint
        ), patch.object(
            document_coordinator, "_current_matching_quotations", return_value=["SQ-1"]
        ), patch.object(
            document_coordinator, "quotation_snapshot_hash", return_value="quote-hash"
        ), patch.object(
            document_coordinator, "_validate_material_request_quotation_authority", return_value=None
        ), patch.object(
            document_coordinator, "_plan_references_material_request", return_value=True
        ), patch.object(
            document_coordinator, "purchase_review_owner", return_value="director@example.com"
        ), patch.object(
            document_coordinator, "inventory_automation_identity", return_value=nullcontext()
        ), patch.object(
            document_coordinator, "_ensure_purchase_order_todo"
        ), patch.object(
            document_coordinator,
            "_record_plan_document",
            side_effect=lambda _plan, ref: created.append(ref),
        ), patch.object(document_coordinator, "_set_if_present"):
            fake_frappe.db.exists.return_value = True
            fake_frappe.get_doc.side_effect = get_doc
            fake_frappe.new_doc.return_value = document
            fake_frappe.DuplicateEntryError = frappe.DuplicateEntryError
            result = document_coordinator.create_draft_purchase_order(
                company="Cafe Co",
                material_request="MAT-1",
                quotation="SQ-1",
                plan_hash="plan-hash",
                policy_hash="policy-hash",
                quotation_hash="quote-hash",
                warehouse="Outlet - JiJi",
            )
        return result, created

    def test_a_replayed_plan_reuses_the_existing_draft(self) -> None:
        """The fingerprint pre-check short-circuits before any document is built."""

        document = _PurchaseOrder("PO-NEW")
        result, created = self._run(
            existing_fingerprint=lambda *_a: "PO-EXISTING", document=document
        )

        self.assertEqual(result["status"], "duplicate")
        self.assertEqual(result["purchase_order"], "PO-EXISTING")
        self.assertEqual(document.insert_calls, 0)
        self.assertEqual(created, [{"doctype": "Purchase Order", "name": "PO-EXISTING"}])

    def test_losing_the_insert_race_recovers_the_winner_instead_of_duplicating(self) -> None:
        """Two runs can pass the pre-check together; only one row may survive.

        The loser's insert raises DuplicateEntryError, and it must adopt the
        winning document rather than retry into a second Draft PO.
        """

        lookups = iter([None, "PO-WINNER"])
        document = _PurchaseOrder("PO-LOSER", duplicate_on_insert=True)
        result, created = self._run(
            existing_fingerprint=lambda *_a: next(lookups), document=document
        )

        self.assertEqual(result["status"], "duplicate")
        self.assertEqual(result["purchase_order"], "PO-WINNER")
        self.assertEqual(document.insert_calls, 1)
        self.assertEqual(created, [{"doctype": "Purchase Order", "name": "PO-WINNER"}])
        self.assertFalse(document.submitted)

    def test_a_lost_race_with_no_recoverable_winner_is_raised_not_duplicated(self) -> None:
        """Never invent a second Draft PO when the winner cannot be identified."""

        document = _PurchaseOrder("PO-LOSER", duplicate_on_insert=True)
        with self.assertRaises(frappe.DuplicateEntryError):
            self._run(existing_fingerprint=lambda *_a: None, document=document)
        self.assertEqual(document.insert_calls, 1)

    def test_the_first_run_creates_exactly_one_unsubmitted_draft(self) -> None:
        document = _PurchaseOrder("PO-FIRST")
        result, created = self._run(existing_fingerprint=lambda *_a: None, document=document)

        self.assertEqual(result["status"], "created_draft")
        self.assertEqual(result["purchase_order"], "PO-FIRST")
        self.assertEqual(result["docstatus"], 0)
        self.assertEqual(document.insert_calls, 1)
        self.assertFalse(document.submitted)
        self.assertEqual(created, [{"doctype": "Purchase Order", "name": "PO-FIRST"}])


if __name__ == "__main__":
    import unittest

    unittest.main()
