from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from kopos_connector.tests.fake_frappe import install_fake_frappe_modules


install_fake_frappe_modules()

import frappe

from kopos_connector.api import inventory as inventory_api


class _PurchaseReceipt:
    def __init__(self, name: str = "PREC-1") -> None:
        self.doctype = "Purchase Receipt"
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


class SupplierReceivingContractTests(TestCase):
    """Accepted stock must post even when the delivery is imperfect."""

    def _purchase_order(self, *, received_qty: str = "0") -> SimpleNamespace:
        return SimpleNamespace(
            name="PO-1",
            modified="2026-08-15 10:00:00",
            docstatus=1,
            company="Cafe Co",
            supplier="Dairy Supplier",
            items=[SimpleNamespace(
                name="PO-ITEM-1",
                item_code="MILK",
                warehouse="Outlet",
                qty=Decimal("10"),
                received_qty=Decimal(received_qty),
                uom="Pack",
                stock_uom="Each",
                conversion_factor=Decimal("12"),
                stock_qty=Decimal("120"),
                rate=Decimal("25"),
            )],
        )

    def _receive(
        self,
        line_overrides: dict[str, object],
        *,
        received_qty: str = "0",
        receipt_name: str = "PREC-1",
    ) -> tuple[_PurchaseReceipt, list[dict[str, object]], str]:
        line = {
            "item_code": "MILK",
            "purchase_order_item": "PO-ITEM-1",
            "warehouse": "Outlet",
            "uom": "Pack",
            "stock_uom": "Each",
            "conversion_factor": "12",
            "batch_no": "BATCH-1",
            "expiry_date": "2026-12-31",
        }
        line.update(line_overrides)
        value = {
            "company": "Cafe Co",
            "warehouse": "Outlet",
            "purchase_order": "PO-1",
            "source_document": "PO-1",
            "source_revision": "2026-08-15T10:00:00+08:00",
            "lines": [line],
        }
        document = _PurchaseReceipt(receipt_name)
        raised: list[dict[str, object]] = []

        with patch.object(inventory_api, "frappe") as fake_frappe, patch.object(
            inventory_api, "_lock_guided_source_document"
        ), patch.object(inventory_api, "_validate_guided_source_revision"), patch.object(
            inventory_api, "_apply_guided_task_audit"
        ), patch.object(inventory_api, "_set_document_value"), patch.object(
            inventory_api, "_require_item_batch_metadata"
        ), patch.object(
            inventory_api,
            "upsert_inventory_exception",
            side_effect=lambda **kwargs: raised.append(kwargs) or "EX-RECEIVE",
        ):
            fake_frappe.db.exists.side_effect = lambda doctype, *_a, **_k: doctype != "Item"
            fake_frappe.get_meta.return_value = SimpleNamespace(has_field=lambda _field: True)
            fake_frappe.get_doc.return_value = self._purchase_order(received_qty=received_qty)
            fake_frappe.new_doc.return_value = document
            fake_frappe.ValidationError = frappe.ValidationError
            fake_frappe.PermissionError = frappe.PermissionError
            fake_frappe._ = lambda value: value
            fake_frappe.throw.side_effect = lambda message, exc=None: (_ for _ in ()).throw(
                (exc or frappe.ValidationError)(message)
            )
            result = inventory_api._create_purchase_receipt(value, "CMD-RECEIVE-1")
        return document, raised, result

    def test_a_clean_delivery_posts_one_receipt_and_no_exception(self) -> None:
        document, raised, result = self._receive({"qty": "10"})

        self.assertEqual(result, "PREC-1")
        self.assertEqual(len(document.items), 1)
        self.assertEqual(document.items[0]["qty"], Decimal("10"))
        self.assertEqual(document.items[0]["purchase_order_item"], "PO-ITEM-1")
        self.assertEqual(document.items[0]["batch_no"], "BATCH-1")
        self.assertEqual(document.items[0]["expiry_date"], "2026-12-31")
        self.assertEqual(raised, [])

    def test_damaged_and_missing_stock_still_posts_the_accepted_quantity(self) -> None:
        document, raised, result = self._receive(
            {"qty": "6", "missing_qty": "1", "damaged_qty": "2"}
        )

        # Accepted stock is not held hostage by the discrepancy.
        self.assertEqual(result, "PREC-1")
        self.assertEqual(document.items[0]["qty"], Decimal("6"))

        self.assertEqual(len(raised), 1)
        self.assertEqual(raised[0]["reason_code"], "supplier_receiving_discrepancy")
        self.assertEqual(raised[0]["item"], "MILK")
        self.assertIn("missing 1", raised[0]["summary"])
        self.assertIn("damaged 2", raised[0]["summary"])

    def test_excess_stays_outside_usable_stock(self) -> None:
        document, raised, result = self._receive({"qty": "10", "excess_qty": "3"})

        # Only the ordered quantity is received; the excess is reported, never
        # silently added to the Purchase Receipt.
        self.assertEqual(document.items[0]["qty"], Decimal("10"))
        self.assertEqual(result, "PREC-1")
        self.assertEqual(len(raised), 1)
        self.assertIn("excess 3", raised[0]["summary"])

    def test_excess_alone_does_not_consume_purchase_order_quantity(self) -> None:
        """Excess must not count against the remaining ordered quantity."""

        _, raised, result = self._receive(
            {"qty": "10", "excess_qty": "5"}, received_qty="0"
        )

        self.assertEqual(result, "PREC-1")
        self.assertEqual(len(raised), 1)

    def test_accepted_plus_damaged_and_missing_cannot_exceed_the_order(self) -> None:
        with self.assertRaises(frappe.ValidationError) as error:
            self._receive({"qty": "8", "missing_qty": "2", "damaged_qty": "1"})
        self.assertIn("exceed the remaining Purchase Order quantity", str(error.exception))

    def test_receiving_is_bounded_by_what_was_already_received(self) -> None:
        with self.assertRaises(frappe.ValidationError) as error:
            self._receive({"qty": "5"}, received_qty="7")
        self.assertIn("exceed the remaining Purchase Order quantity", str(error.exception))

    def test_a_fully_rejected_delivery_posts_no_receipt_but_opens_an_exception(self) -> None:
        document, raised, result = self._receive({"qty": "0", "damaged_qty": "10"})

        self.assertEqual(document.items, [])
        self.assertEqual(result, "EX-RECEIVE")
        self.assertEqual(len(raised), 1)
        self.assertEqual(raised[0]["reason_code"], "supplier_receiving_discrepancy")

    def test_receiving_outside_the_device_warehouse_is_refused(self) -> None:
        with self.assertRaises(frappe.PermissionError) as error:
            self._receive({"qty": "10", "warehouse": "Other Outlet"})
        self.assertIn("cannot post outside the device warehouse", str(error.exception))

    def test_a_repeated_purchase_order_row_is_refused_in_one_command(self) -> None:
        value = {
            "company": "Cafe Co",
            "warehouse": "Outlet",
            "purchase_order": "PO-1",
            "source_document": "PO-1",
            "source_revision": "2026-08-15T10:00:00+08:00",
            "lines": [
                {
                    "item_code": "MILK",
                    "purchase_order_item": "PO-ITEM-1",
                    "warehouse": "Outlet",
                    "uom": "Pack",
                    "stock_uom": "Each",
                    "conversion_factor": "12",
                    "qty": "4",
                }
            ]
            * 2,
        }
        with patch.object(inventory_api, "frappe") as fake_frappe, patch.object(
            inventory_api, "_lock_guided_source_document"
        ), patch.object(inventory_api, "_validate_guided_source_revision"), patch.object(
            inventory_api, "_apply_guided_task_audit"
        ), patch.object(inventory_api, "_set_document_value"), patch.object(
            inventory_api, "_require_item_batch_metadata"
        ), patch.object(inventory_api, "upsert_inventory_exception", return_value="EX"):
            fake_frappe.db.exists.side_effect = lambda doctype, *_a, **_k: doctype != "Item"
            fake_frappe.get_meta.return_value = SimpleNamespace(has_field=lambda _field: True)
            fake_frappe.get_doc.return_value = self._purchase_order()
            fake_frappe.new_doc.return_value = _PurchaseReceipt()
            fake_frappe.ValidationError = frappe.ValidationError
            fake_frappe.PermissionError = frappe.PermissionError
            fake_frappe._ = lambda value: value
            fake_frappe.throw.side_effect = lambda message, exc=None: (_ for _ in ()).throw(
                (exc or frappe.ValidationError)(message)
            )
            with self.assertRaises(frappe.ValidationError) as error:
                inventory_api._create_purchase_receipt(value, "CMD-RECEIVE-DUP")
        self.assertIn("Receive each Purchase Order row once", str(error.exception))


if __name__ == "__main__":
    import unittest

    unittest.main()
