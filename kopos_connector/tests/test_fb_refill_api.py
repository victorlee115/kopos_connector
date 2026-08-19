from __future__ import annotations

from pathlib import Path
from decimal import Decimal
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from kopos_connector.tests.fake_frappe import install_fake_frappe_modules


install_fake_frappe_modules()

import frappe  # noqa: E402

from kopos_connector.api import fb_refill  # noqa: E402
from kopos_connector.kopos.doctype.fb_booth_refill_request.fb_booth_refill_request import (  # noqa: E402
    FBBoothRefillRequest,
)


class DuplicateEntryError(Exception):
    pass


class FakeMaterialRequest:
    def __init__(self, database: "FakeRefillDatabase") -> None:
        self.doctype = "Material Request"
        self.name: str | None = None
        self.docstatus = 0
        self.company = None
        self.material_request_type = None
        self.transaction_date = None
        self.schedule_date = None
        self.custom_kopos_inventory_fingerprint = None
        self.set_warehouse = None
        self.set_from_warehouse = None
        self.remarks = None
        self.items: list[SimpleNamespace] = []
        self.database = database

    def append(self, fieldname: str, values: dict[str, object]) -> None:
        assert fieldname == "items"
        self.items.append(SimpleNamespace(**values))

    def insert(self, ignore_permissions: bool = False) -> "FakeMaterialRequest":
        del ignore_permissions
        fingerprint = self.custom_kopos_inventory_fingerprint
        if fingerprint in self.database.requests_by_fingerprint:
            raise DuplicateEntryError(fingerprint)
        self.name = f"MAT-REQ-{len(self.database.requests) + 1}"
        self.database.requests[self.name] = self
        self.database.requests_by_fingerprint[fingerprint] = self.name
        return self


class FakeRefillDatabase:
    def __init__(self) -> None:
        self.requests: dict[str, FakeMaterialRequest] = {}
        self.requests_by_fingerprint: dict[str, str] = {}
        self.commit_count = 0

    def exists(self, doctype: str, name: object = None) -> bool:
        if doctype == "Item":
            return name == "ITEM-1"
        if doctype == "DocType":
            return name in {"Material Request", "Material Request Item"}
        return False

    def get_value(self, doctype: str, filters: object, fieldname: str) -> object:
        if doctype == "Warehouse" and fieldname == "company":
            return "Cafe Co" if filters == "SOURCE-WH" else None
        if doctype == "Item" and fieldname == "stock_uom":
            return "Each"
        if doctype == "UOM Conversion Detail" and fieldname == "conversion_factor":
            return "12.5"
        if doctype == "Material Request" and fieldname == "name":
            assert isinstance(filters, dict)
            return self.requests_by_fingerprint.get(
                filters.get("custom_kopos_inventory_fingerprint")
            )
        return None

    def commit(self) -> None:
        self.commit_count += 1


class FakeFrappe:
    DuplicateEntryError = DuplicateEntryError
    ValidationError = frappe.ValidationError
    form_dict: dict[str, object] = {}
    request = None
    db = FakeRefillDatabase()

    class _Meta:
        @staticmethod
        def has_field(_fieldname: str) -> bool:
            return True

    @staticmethod
    def get_meta(_doctype: str) -> "FakeFrappe._Meta":
        return FakeFrappe._Meta()

    @staticmethod
    def new_doc(doctype: str) -> FakeMaterialRequest:
        assert doctype == "Material Request"
        return FakeMaterialRequest(FakeFrappe.db)

    @staticmethod
    def get_doc(_doctype: str, name: str) -> FakeMaterialRequest:
        return FakeFrappe.db.requests[name]

    @staticmethod
    def throw(message: str, exception: type[Exception] | None = None) -> None:
        raise (exception or FakeFrappe.ValidationError)(message)


def _payload(qty: str = "2.5") -> dict[str, object]:
    return {
        "request_id": "REFILL-1",
        "device_id": "DEVICE-1",
        "company": "Cafe Co",
        "from_warehouse": "SOURCE-WH",
        "to_warehouse": "OUTLET-WH",
        "lines": [{"item": "ITEM-1", "qty": qty, "uom": "Pack"}],
    }


class TestFBRefillAPI(TestCase):
    def setUp(self) -> None:
        FakeFrappe.db = FakeRefillDatabase()
        self.patches = [
            patch.object(fb_refill, "frappe", FakeFrappe),
            patch.object(fb_refill, "lock_device_for_operational_mutation"),
            patch.object(fb_refill, "require_device_operational_scope"),
        ]
        for active_patch in self.patches:
            active_patch.start()

    def tearDown(self) -> None:
        for active_patch in reversed(self.patches):
            active_patch.stop()

    def test_creates_one_draft_standard_material_transfer(self) -> None:
        FakeFrappe.form_dict = _payload()

        result = fb_refill.process_refill()

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["material_request"], "MAT-REQ-1")
        self.assertEqual(result["refill_request"], "MAT-REQ-1")
        self.assertEqual(result["docstatus"], 0)
        document = FakeFrappe.db.requests["MAT-REQ-1"]
        self.assertEqual(document.material_request_type, "Material Transfer")
        self.assertEqual(document.items[0].from_warehouse, "SOURCE-WH")
        self.assertEqual(document.items[0].stock_uom, "Each")
        self.assertEqual(document.items[0].conversion_factor, Decimal("12.5"))
        self.assertEqual(FakeFrappe.db.commit_count, 1)

    def test_exact_retry_replays_without_a_second_document(self) -> None:
        FakeFrappe.form_dict = _payload()
        first = fb_refill.process_refill()

        second = fb_refill.process_refill()

        self.assertEqual(first["material_request"], second["material_request"])
        self.assertEqual(second["status"], "replayed")
        self.assertEqual(len(FakeFrappe.db.requests), 1)
        self.assertEqual(FakeFrappe.db.commit_count, 1)

    def test_reused_request_id_with_changed_quantity_is_rejected(self) -> None:
        FakeFrappe.form_dict = _payload()
        fb_refill.process_refill()
        FakeFrappe.form_dict = _payload(qty="3")

        with self.assertRaisesRegex(
            FakeFrappe.ValidationError, "reused with different refill content"
        ):
            fb_refill.process_refill()

        self.assertEqual(len(FakeFrappe.db.requests), 1)

    def test_invalid_decimal_is_rejected_before_document_creation(self) -> None:
        FakeFrappe.form_dict = _payload(qty="NaN")

        with self.assertRaises(FakeFrappe.ValidationError):
            fb_refill.process_refill()

        self.assertEqual(FakeFrappe.db.requests, {})

    def test_source_warehouse_must_belong_to_submitted_company(self) -> None:
        invalid = _payload()
        invalid["company"] = "Another Co"
        FakeFrappe.form_dict = invalid

        with self.assertRaisesRegex(FakeFrappe.ValidationError, "outside company"):
            fb_refill.process_refill()

        self.assertEqual(FakeFrappe.db.requests, {})

    def test_endpoint_has_no_legacy_submit_or_stock_entry_creation(self) -> None:
        source = (Path(__file__).parents[1] / "api" / "fb_refill.py").read_text()
        self.assertNotIn('new_doc("FB Booth Refill Request")', source)
        self.assertNotIn(".submit(", source)
        self.assertNotIn('new_doc("Stock Entry")', source)

    def test_legacy_refill_document_cannot_move_stock_on_submit(self) -> None:
        with self.assertRaisesRegex(frappe.ValidationError, "Legacy refill submission is retired"):
            FBBoothRefillRequest.on_submit(SimpleNamespace())


if __name__ == "__main__":
    import unittest

    unittest.main()
