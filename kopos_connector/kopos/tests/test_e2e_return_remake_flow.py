from __future__ import annotations

from unittest.mock import patch

import frappe
import pytest
from frappe.tests.utils import FrappeTestCase

pytestmark = pytest.mark.inventory_regression


class SaleBackedOperationsTestCase(FrappeTestCase):
    def setUp(self) -> None:
        from kopos_connector.kopos.api.fb_orders import submit_order_payload
        from kopos_connector.kopos.services.inventory_autopilot.projection_worker import (
            _prepare_frozen_resolved_sales,
        )
        from kopos_connector.kopos.tests.frappe_test_fixtures import (
            build_sen_v1_sale_payload,
            create_open_test_shift,
        )

        self.addCleanup(frappe.db.rollback)
        frappe.set_user("Administrator")
        self.shift = create_open_test_shift(
            prefix="KOPOS-E2E-OPS", replenish_stock=True
        )
        sale_payload = build_sen_v1_sale_payload(
            self.shift,
            prefix="KOPOS-E2E-OPS",
            include_optional_recipe=True,
        )
        sale_result = submit_order_payload(sale_payload)
        self.order = frappe.get_doc("FB Order", sale_result["fb_order"])
        # The production inventory worker prepares this immutable sale-time
        # recipe snapshot after checkout commits. Keep the integration fixture
        # inside FrappeTestCase's rollback boundary while exercising the same
        # preparation implementation.
        with patch.object(frappe.db, "commit"):
            _prepare_frozen_resolved_sales(self.order)
        resolved_sales = frappe.get_all(
            "FB Resolved Sale",
            filters={"fb_order": self.order.name},
            pluck="name",
        )
        self.assertEqual(len(resolved_sales), 1)
        self.resolved_sale = frappe.get_doc("FB Resolved Sale", resolved_sales[0])


class TestEndToEndRemakeFlow(SaleBackedOperationsTestCase):
    def test_remake_creates_submitted_stock_issue_without_revenue(self) -> None:
        from kopos_connector.api.fb_remakes import process_remake_payload

        invoices_before = set(
            frappe.get_all(
                "Sales Invoice",
                filters={"custom_fb_order": self.order.name},
                pluck="name",
            )
        )
        payload = {
            "remake_id": f"KOPOS-E2E-REMAKE-{frappe.generate_hash(length=12)}",
            "device_id": self.order.device_id,
            "original_order": self.order.name,
            "original_order_line": self.order.items[0].line_id,
            "quantity": self.order.items[0].qty,
            "reason_code": "Spill",
            "reason_text": "Real-Frappe acceptance fixture",
        }

        result = process_remake_payload(payload, require_manager_approval=False)
        duplicate = process_remake_payload(payload, require_manager_approval=False)

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["inventory_evaluation"], "included_evaluated")
        self.assertEqual(result["inventory_status"], "Posted")
        self.assertEqual(duplicate["status"], "duplicate")
        self.assertEqual(duplicate["remake_event"], result["remake_event"])
        self.assertEqual(
            duplicate["replacement_stock_entry"], result["replacement_stock_entry"]
        )
        remake_event = frappe.get_doc("FB Remake Event", result["remake_event"])
        self.assertEqual(remake_event.docstatus, 1)
        self.assertEqual(remake_event.quantity, self.order.items[0].qty)
        self.assertEqual(remake_event.original_resolved_sale, self.resolved_sale.name)
        stock_entry = frappe.get_doc("Stock Entry", result["replacement_stock_entry"])
        self.assertEqual(stock_entry.docstatus, 1)
        self.assertEqual(stock_entry.stock_entry_type, "Material Issue")
        self.order.reload()
        self.assertEqual(self.order.items[0].remade_qty, self.order.items[0].qty)
        invoices_after = set(
            frappe.get_all(
                "Sales Invoice",
                filters={"custom_fb_order": self.order.name},
                pluck="name",
            )
        )
        self.assertEqual(invoices_after, invoices_before)

    def test_remake_exception_recovers_after_sale_snapshot_becomes_ready(self) -> None:
        from kopos_connector.api.fb_remakes import process_remake_payload
        from kopos_connector.kopos.services.inventory_autopilot.projection_worker import (
            recover_remake_inventory_projections,
        )

        line = self.order.items[0]
        frappe.db.set_value(
            "FB Order Line",
            line.name,
            "resolved_sale",
            None,
            update_modified=False,
        )
        result = process_remake_payload(
            {
                "remake_id": f"KOPOS-E2E-REMAKE-EX-{frappe.generate_hash(length=12)}",
                "device_id": self.order.device_id,
                "original_order": self.order.name,
                "original_order_line": line.line_id,
                "quantity": line.qty,
                "reason_code": "Quality Issue",
                "reason_text": "Missing inventory evidence acceptance fixture",
            },
            require_manager_approval=False,
        )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["inventory_status"], "Exception")
        self.assertIsNone(result["replacement_stock_entry"])
        self.assertTrue(result["inventory_exception"])
        event = frappe.get_doc("FB Remake Event", result["remake_event"])
        self.assertEqual(event.docstatus, 1)
        self.assertEqual(event.inventory_status, "Exception")
        self.assertTrue(
            frappe.db.exists("FB Inventory Exception", result["inventory_exception"])
        )

        # Model the real checkout/remake race: the optional sale worker records
        # the immutable snapshot after the operational remake already committed.
        frappe.db.set_value(
            "FB Order Line",
            line.name,
            "resolved_sale",
            self.resolved_sale.name,
            update_modified=False,
        )
        candidates = frappe.get_all(
            "FB Remake Event",
            filters={
                "docstatus": 1,
                "inventory_status": ["in", ["Pending", "Exception"]],
            },
            pluck="name",
        )
        self.assertIn(event.name, candidates)
        projection_log_before_recovery = frappe.db.get_value(
            "FB Projection Log",
            {
                "source_doctype": "FB Remake Event",
                "source_name": event.name,
                "projection_type": "Stock Issue",
            },
            "name",
        )
        self.assertFalse(projection_log_before_recovery)
        # Keep FrappeTestCase's rollback boundary intact. Production commits
        # each recovered projection; this case verifies the real documents
        # without leaking its transaction into the rest of the broad suite.
        with patch.object(frappe.db, "commit"):
            recovery = recover_remake_inventory_projections(batch_size=1)

        self.assertEqual(len(recovery), 1)
        self.assertEqual(recovery[0]["state"], "Succeeded")
        event.reload()
        self.assertEqual(event.inventory_status, "Posted")
        self.assertTrue(event.replacement_stock_entry)
        stock_entry = frappe.get_doc("Stock Entry", event.replacement_stock_entry)
        self.assertEqual(stock_entry.docstatus, 1)
        self.assertEqual(stock_entry.stock_entry_type, "Material Issue")
        self.assertEqual(
            frappe.db.get_value(
                "FB Inventory Exception",
                result["inventory_exception"],
                "status",
            ),
            "Resolved",
        )
        self.assertEqual(
            frappe.db.get_value(
                "FB Projection Log",
                {
                    "source_doctype": "FB Remake Event",
                    "source_name": event.name,
                    "projection_type": "Stock Issue",
                },
                "state",
            ),
            "Succeeded",
        )
