from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from kopos_connector.api.fb_returns import process_return_payload
from kopos_connector.kopos.api.fb_orders import submit_order_payload
from kopos_connector.kopos.tests.frappe_test_fixtures import (
    build_sen_v1_sale_payload,
    create_open_test_shift,
)


class TestEndToEndCommercialRefundFlow(FrappeTestCase):
    def setUp(self) -> None:
        frappe.set_user("Administrator")
        self.shift = create_open_test_shift(
            prefix="KOPOS-E2E-COMMERCIAL-RETURN",
            replenish_stock=False,
        )
        self.sale_payload = build_sen_v1_sale_payload(
            self.shift,
            prefix="KOPOS-E2E-COMMERCIAL-RETURN",
        )
        sale_result = submit_order_payload(self.sale_payload)
        self.order = frappe.get_doc("FB Order", sale_result["fb_order"])
        self.return_id = (
            "KOPOS-E2E-COMMERCIAL-RETURN-"
            + frappe.generate_hash(length=12)
        )

    def tearDown(self) -> None:
        frappe.db.rollback()

    def _payload(self, *, return_to_stock: bool) -> dict[str, object]:
        return {
            "return_id": self.return_id,
            "idempotency_key": self.return_id,
            "device_id": self.order.device_id,
            "fb_order": self.order.name,
            "original_sales_invoice": self.order.sales_invoice,
            "reason_code": "Quality Issue",
            "reason_text": "Commercial refund round-trip fixture",
            "refund_method": "cash",
            "return_to_stock": 1 if return_to_stock else 0,
        }

    def test_full_refund_round_trips_without_resolved_sale_or_stock_calls(self) -> None:
        self.assertEqual(
            frappe.get_all(
                "FB Resolved Sale",
                filters={"fb_order": self.order.name},
                pluck="name",
            ),
            [],
        )

        with patch(
            "kopos_connector.kopos.services.inventory.stock_reversal_service.create_reversal_stock_entry",
            side_effect=AssertionError("cashier refund called inventory"),
        ):
            result = process_return_payload(
                self._payload(return_to_stock=True),
                require_manager_approval=False,
            )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["settlement_status"], "Posted")
        self.assertEqual(result["return_to_stock"], 0)
        self.assertEqual(
            result["inventory_evaluation"],
            "excluded_not_evaluated",
        )
        self.assertEqual(result["reversal_stock_entries"], [])

        return_event = frappe.get_doc("FB Return Event", result["return_event"])
        return_event.reload()
        self.assertEqual(return_event.docstatus, 1)
        self.assertEqual(return_event.return_to_stock, 0)
        self.assertEqual(len(return_event.lines), len(self.order.items))
        self.assertTrue(
            all(line.original_sales_invoice_item for line in return_event.lines)
        )
        self.assertTrue(
            all(line.original_fb_order_line_ref for line in return_event.lines)
        )
        self.assertTrue(
            all(
                not line.commercial_modifier_snapshot_json
                for line in return_event.lines
            )
        )
        self.assertTrue(
            all(not line.original_resolved_sale for line in return_event.lines)
        )

        original_invoice = frappe.get_doc(
            "Sales Invoice", self.order.sales_invoice
        )
        return_invoice = frappe.get_doc(
            "Sales Invoice", result["return_sales_invoice"]
        )
        self.assertEqual(return_invoice.docstatus, 1)
        self.assertEqual(return_invoice.is_return, 1)
        self.assertEqual(return_invoice.return_against, original_invoice.name)
        self.assertEqual(return_invoice.outstanding_amount, 0)
        self.assertEqual(
            {line.sales_invoice_item for line in return_invoice.items},
            {line.name for line in original_invoice.items},
        )
        self.assertEqual(
            {line.custom_fb_order_line_ref for line in return_invoice.items},
            {line.custom_fb_order_line_ref for line in original_invoice.items},
        )

        settlement = frappe.get_doc(
            result["settlement_doctype"], result["settlement_document"]
        )
        self.assertEqual(settlement.docstatus, 1)
        self.assertEqual(settlement.custom_fb_return_event, return_event.name)

    def test_idempotent_replay_reuses_one_credit_note_and_settlement(self) -> None:
        payload = self._payload(return_to_stock=False)
        first = process_return_payload(payload, require_manager_approval=False)
        duplicate = process_return_payload(payload, require_manager_approval=False)

        self.assertEqual(first["status"], "ok")
        self.assertEqual(duplicate["status"], "duplicate")
        for fieldname in (
            "return_event",
            "return_sales_invoice",
            "settlement_doctype",
            "settlement_document",
        ):
            self.assertEqual(duplicate[fieldname], first[fieldname])
        self.assertEqual(
            frappe.get_all(
                "FB Return Event",
                filters={"return_id": self.return_id},
                pluck="name",
            ),
            [first["return_event"]],
        )
        self.assertEqual(
            frappe.get_all(
                "Sales Invoice",
                filters={
                    "name": first["return_sales_invoice"],
                    "docstatus": 1,
                    "is_return": 1,
                },
                pluck="name",
            ),
            [first["return_sales_invoice"]],
        )
        self.assertEqual(
            frappe.get_all(
                "Journal Entry",
                filters={
                    "custom_fb_return_event": first["return_event"],
                    "docstatus": 1,
                },
                pluck="name",
            ),
            [first["settlement_document"]],
        )

    def test_historical_invoice_without_order_line_reference_refunds_without_optional_recipe_rows(
        self,
    ) -> None:
        original_invoice = frappe.get_doc(
            "Sales Invoice", self.order.sales_invoice
        )
        for item in original_invoice.items:
            frappe.db.set_value(
                "Sales Invoice Item",
                item.name,
                {
                    "custom_fb_order_line_ref": None,
                    "custom_fb_resolved_sale": "STALE-OPTIONAL-IDENTITY",
                    "custom_kopos_modifiers": "{corrupt optional decoration",
                },
                update_modified=False,
            )
        for item in self.order.items:
            frappe.db.set_value(
                "FB Order Line",
                item.name,
                "commercial_modifier_snapshot_json",
                "{corrupt optional decoration",
                update_modified=False,
            )

        with patch(
            "kopos_connector.kopos.services.operations.return_guard_service._lock_and_validate_legacy_return_quantities",
            side_effect=AssertionError("cashier refund used legacy recipe identity"),
        ):
            result = process_return_payload(
                self._payload(return_to_stock=False),
                require_manager_approval=False,
            )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["settlement_status"], "Posted")
        return_event = frappe.get_doc("FB Return Event", result["return_event"])
        self.assertTrue(
            all(line.original_sales_invoice_item for line in return_event.lines)
        )
        self.assertTrue(
            all(not line.original_fb_order_line_ref for line in return_event.lines)
        )
        self.assertTrue(
            all(not line.original_resolved_sale for line in return_event.lines)
        )
